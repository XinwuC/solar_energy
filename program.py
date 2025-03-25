import json
import logging
import logging.config
import os
from datetime import datetime, timedelta
from time import sleep

import pyemvue
import pypowerwall
from dateutil import parser, tz
from suntime import Sun

from fordconnect import FordConnect


class SolarHome:
    def __init__(self, params: dict) -> None:
        self.logger = logging.getLogger(__name__)

        # set start and stop time
        self.time_zone = tz.gettz("America/Los_Angeles")
        self.sunrise, self.sunset = self.sunrise_sunset()
        self.nem_peak_hour = parser.parse("15:00").astimezone(self.time_zone)
        self.excessive_ratio = params.get("excessive_ratio") or 0.98
        self.max_soc_on_grid = params.get("max_soc_on_grid") or 60
        self.api_refresh_interval = timedelta(seconds=60)

        # ford ev
        self.ford = FordConnect(params["ford"])
        self.vehicle_soc = self.max_soc_on_grid
        self.vehicle_soc_update_time = datetime.today().astimezone(self.time_zone) - timedelta(days=1)
        self.vehicle_id = None

        # powerwall
        self.powerwall_host = params["powerwall"]["host"]
        self.powerwall_user = params["powerwall"]["user"]
        self.powerwall_password = params["powerwall"]["password"]
        self.powerwall = None

        # emporia
        self.emporia_user = str(params["emporia"]["user"])
        self.emporia_password = str(params["emporia"]["password"])
        self.emporia_token_file = "keys.json"
        self.emporia = pyemvue.PyEmVue()
        self.evse_refresh_time = datetime.today().astimezone(self.time_zone) - timedelta(days=1)
        self.evse = None
        self.emporia_vehicle = None

        self.min_excessive_solar = int(6 * 240)
        self.min_charging_state_change_interval = timedelta(minutes=5)
        self.last_charging_state_change = datetime.now(tz=self.time_zone) - self.min_charging_state_change_interval

    def sunrise_sunset(self):
        sun = Sun(37.32, -122.03)
        sunrise = sun.get_sunrise_time(time_zone=self.time_zone)
        sunset = sun.get_sunset_time(time_zone=self.time_zone)
        # temp fix for sunset is yesterday
        sunset = datetime.combine(sunrise, sunset.time(), tzinfo=self.time_zone)
        return sunrise, sunset

    def login_emporia(self) -> bool:
        loggedin = False
        if os.path.exists(self.emporia_token_file):
            try:
                with open(self.emporia_token_file) as f:
                    token = json.load(f)
                    loggedin = self.emporia.login(
                        id_token=token["id_token"],
                        access_token=token["access_token"],
                        refresh_token=token["refresh_token"],
                        token_storage_file=self.emporia_token_file,
                    )
            except Exception as e:
                os.remove(self.emporia_token_file)
                self.logger.exception(e)

        if not loggedin:
            loggedin = self.emporia.login(
                username=self.emporia_user,
                password=self.emporia_password,
                token_storage_file=self.emporia_token_file,
            )
        self.logger.info("Logged into Emporia EVSE: %s" % loggedin)
        return loggedin

    def login_powerwall(self) -> bool:
        connected = True
        if not self.powerwall or not self.powerwall.is_connected():
            self.powerwall = pypowerwall.Powerwall(
                host=self.powerwall_host,
                email=self.powerwall_user,
                password=self.powerwall_password
            )
            connected = self.powerwall.is_connected()
            self.logger.info(f"Connect to Tesla Powerwall: {connected}")
        return connected

    def available_solar(self) -> int:
        # get stats from powerwall
        if self.login_powerwall():
            power = self.powerwall.power()
            self.powerwall.solar = power["solar"]
            self.powerwall.battery = power["battery"]
            self.powerwall.home = power["load"]

        else:
            self.powerwall.solar = 0
            self.powerwall.battery = 0
            self.powerwall.home = 0
        # calculate available solar
        available = self.powerwall.solar - self.powerwall.home - abs(self.powerwall.battery)
        self.logger.debug(
            f"Available solar: {available:,.0f}w "
            f"[Solar: {self.powerwall.solar:,.0f}w; "
            f"Home: {self.powerwall.home:,.0f}w; "
            f"Battery: {self.powerwall.battery:,.0f}w]")
        return int(available)

    def solar_charge(self) -> bool:
        """
        return True if charging on excessive solar, else False
        """
        self.refresh_charger_status()
        if not self.is_car_connected():
            self.logger.debug(f"EV charger is not plugged in: {self.evse.icon}.")
            return self.evse.charger_on

        excessive = self.available_solar() + self.evse.charger_on * self.evse.charging_rate * 240
        if excessive > self.min_excessive_solar:
            charge_rate = int(max(min(excessive * self.excessive_ratio / 240, 40), 6))
            if self.set_charger(charge_rate):
                self.logger.info(f"Charging at {self.evse.charging_rate}A with excessive solar {excessive:,}w")
        else:
            self.logger.info(f"Excessive solar is not enough: {excessive:,d}w, min: {self.min_excessive_solar:,d}w")
            self.stop_charger()
        return self.evse.charger_on

    def grid_charge(self) -> bool:
        """
        return True if charging on grid, else False
        """
        self.refresh_charger_status()
        if not self.is_car_connected():
            self.logger.debug(f"EV charger is not plugged in: {self.evse.icon}.")
        elif self.refresh_ev_soc() < self.max_soc_on_grid:
            if self.set_charger(40):
                self.logger.info(f"Charge at max rate {self.evse.charging_rate}A on grid.")
        else:
            self.logger.info(
                f"EV SOC is {self.vehicle_soc}%, larger than target {self.max_soc_on_grid}%.")
            self.stop_charger()

        return self.evse.charger_on

    def set_charger(self, charge_rate: int) -> bool:
        """
        return True if charge state changed, either charge on/off or charge rate change
        """
        charge_rate = max(min(charge_rate, 40), 6)
        wait = self.charger_protection_wait()

        self.refresh_charger_status()
        if not self.evse.charger_on and wait > 0:
            self.logger.info("Charger protection: wait %d seconds to charge." % wait)
            return False

        if self.evse.charger_on and self.evse.charging_rate == charge_rate:
            self.logger.debug(f"No change for charging rate @ {charge_rate}A")
            return False

        if not self.evse.charger_on:
            self.last_charging_state_change = datetime.now(tz=self.time_zone)
        self.evse.charger_on = True
        self.evse.charging_rate = charge_rate
        self.evse.max_charging_rate = 40
        self.refresh_charger_status(self.emporia.update_charger(self.evse))
        return self.evse.charger_on

    def charger_protection_wait(self) -> int:
        interval = datetime.now(tz=self.time_zone) - self.last_charging_state_change
        return max(0, self.min_charging_state_change_interval.seconds - interval.seconds)

    def stop_charger(self, reset: bool = False) -> bool:
        """
        return True if charger stopped
        """
        self.refresh_charger_status()
        wait = self.charger_protection_wait()
        if wait > 0:
            self.logger.info(f"Wait {wait} seconds before stop and lower to min charging rate.")
            self.refresh_charger_status(self.emporia.update_charger(self.evse, charge_rate=6))
            sleep(wait)
        if self.evse.charger_on or reset:
            self.evse.charging_rate = 40
            self.evse.charger_on = False
            self.refresh_charger_status(self.emporia.update_charger(self.evse))
            self.last_charging_state_change = datetime.now(tz=self.time_zone)
            self.logger.info(
                f"Charging stopped and reset to 40A, sleep for {self.min_charging_state_change_interval.seconds} seconds!")
            sleep(self.min_charging_state_change_interval.seconds)
        return not self.evse.charger_on

    def is_car_connected(self) -> bool:
        return self.evse.icon == "CarConnected"

    def refresh_charger_status(self, evse=None):
        if evse is not None:
            self.evse = evse
            self.evse_refresh_time = datetime.now(tz=self.time_zone)
        elif datetime.now(tz=self.time_zone) - self.evse_refresh_time > self.api_refresh_interval:
            self.evse = self.emporia.get_chargers()[0]
            self.logger.debug("refresh charger status.")
            self.evse_refresh_time = datetime.now(tz=self.time_zone)
            # fix emporia status when in standby mode
            if self.is_car_connected() and self.evse.status == 'Standby' and self.evse.charger_on:
                self.logger.info("Charger is standby and connected, check if car refuse charging.")
                self.evse.charging_rate = 0
                self.evse.charger_on = False

    def refresh_ev_soc(self, source="emporia") -> float:
        if datetime.now(tz=self.time_zone) - self.vehicle_soc_update_time > self.ford.refresh_interval:
            try:
                if source == "emporia":
                    if self.emporia_vehicle is None:
                        self.emporia_vehicle = self.emporia.get_vehicles()[0]
                    self.vehicle_soc = self.emporia.get_vehicle_status(self.emporia_vehicle.vehicle_gid).battery_level
                elif source == "fordpass":
                    if self.vehicle_id is None:
                        self.vehicle_id = self.ford.vehicle_ids()[0]["vehicleId"]
                        self.logger.info("Get vehicle id: %s" % self.vehicle_id)
                    info = self.ford.vehicle_info(self.vehicle_id)
                    self.vehicle_soc = info["vehicleDetails"]["batteryChargeLevel"]["value"]
                self.vehicle_soc_update_time = datetime.now(tz=self.time_zone)
                self.logger.info("EV SOC @ %d%%" % self.vehicle_soc)
            except Exception as e:
                self.logger.exception(e)
        return self.vehicle_soc

    def run_charger(self, charging: ()):
        try:
            charging()
        except Exception as e:
            self.logger.exception(e)
            self.login_emporia()
        finally:
            sleep(15)

    def run(self):
        self.login_emporia()
        try:
            # charge on grid when solar is unavailable
            while datetime.now(tz=self.time_zone) < self.sunrise:
                self.run_charger(self.grid_charge)
            # smart charge on grid or solar during off-peak hours during solar
            while datetime.now(tz=self.time_zone) < self.nem_peak_hour:
                # charge powerwall battery first then charge the car from grid
                self.run_charger(
                    lambda: self.grid_charge() if not self.solar_charge() and self.powerwall.battery >= 0 else False)
            # charge on solar durin peak hours
            while datetime.now(tz=self.time_zone) < self.sunset:
                self.run_charger(self.solar_charge)
            self.logger.info("Stop running at sunset time: %s." % self.sunset)
        except Exception as e:
            self.logger.exception(e)
        finally:
            self.stop_charger(True)


if __name__ == "__main__":
    with open("logging_config.json", "r") as f:
        logging.config.dictConfig(json.load(f))

    with open("program.json", "r") as f:
        params = json.load(f)

    home = SolarHome(params=params)
    home.run()
