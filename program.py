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
        self.evse = None

        self.min_excessive_solar = int(6 * 240 * 1.05)
        self.min_charging_state_change_interval = timedelta(minutes=5)
        self.last_charging_state_change = datetime.now(tz=self.time_zone) - self.min_charging_state_change_interval

    def sunrise_sunset(self):
        sun = Sun(37.32, -122.03)
        sunrise = sun.get_sunrise_time(time_zone=self.time_zone)
        sunset = sun.get_sunset_time(time_zone=self.time_zone)
        # temp fix for sunset is yesterday
        sunset = datetime.combine(sunrise, sunset.time(), tzinfo=self.time_zone)
        return sunrise, sunset

    def login_powerwall(self) -> bool:
        if not self.powerwall or not self.powerwall.is_connected():
            self.powerwall = pypowerwall.Powerwall(
                host=self.powerwall_host,
                email=self.powerwall_user,
                password=self.powerwall_password
            )
            self.logger.info(f"Connect to Tesla Powerwall: {self.powerwall.is_connected()}")
        return self.powerwall.is_connected()

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

    def available_solar(self) -> int:
        available = 0
        if self.login_powerwall():
            solar = self.powerwall.solar()
            battery = self.powerwall.battery()
            home = self.powerwall.home()
            available = solar - home - abs(battery)
            self.logger.debug(
                f"Available solar: {available:,.0f}w [Solar: {solar:,.0f}w; Home: {home:,.0f}w; Battery: {battery:,.0f}w]")
        return int(available)

    def refresh_charger_status(self):
        self.evse = self.emporia.get_chargers()[0]
        # fix emporia status when in standby mode
        if self.evse.status == 'Standby':
            self.evse.charging_rate = 0
            self.evse.charger_on = False

    def solar_charge(self) -> bool:
        """
        return True if charging on excessive solar, else False
        """
        self.refresh_charger_status()
        if self.evse.icon != "CarConnected":
            self.logger.debug(f"EV charger is not plugged in: {self.evse.icon}.")
            return self.evse.charger_on

        excessive = self.available_solar() + self.evse.charger_on * self.evse.charging_rate * 240
        if self.powerwall.is_connected() and excessive > self.min_excessive_solar:
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
        if self.evse.icon != "CarConnected":
            self.logger.debug(f"EV charger is not plugged in: {self.evse.icon}.")
        elif self.refresh_ev_soc() < self.max_soc_on_grid:
            if self.set_charger(40):
                self.logger.info("Charge at max rate 40A on grid.")
        else:
            self.logger.info(
                f"EV SOC is {self.vehicle_soc}%, larger than target {self.max_soc_on_grid}%.")
            self.stop_charger()

        return self.evse.charger_on

    def set_charger(self, charge_rate: int) -> bool:
        """
        return True if charging
        """
        charge_rate = max(min(charge_rate, 40), 6)
        wait = self.charger_protection_wait()

        self.refresh_charger_status()
        if not self.evse.charger_on and wait > 0:
            self.logger.info("Charger protection: wait %d seconds to charge." % wait)
            return self.evse.charger_on

        if self.evse.charger_on and self.evse.charging_rate == charge_rate:
            self.logger.debug(f"No change for charging rate @ {charge_rate}A")
            return self.evse.charger_on

        if not self.evse.charger_on:
            self.last_charging_state_change = datetime.now(tz=self.time_zone)
        self.evse.charger_on = True
        self.evse.charging_rate = charge_rate
        self.evse.max_charging_rate = 40
        self.evse = self.emporia.update_charger(self.evse)
        return self.evse.charger_on

    def charger_protection_wait(self) -> int:
        interval = datetime.now(tz=self.time_zone) - self.last_charging_state_change
        return max(0, self.min_charging_state_change_interval.seconds - interval.seconds)

    def stop_charger(self) -> bool:
        """
        return True if charger stopped
        """
        self.refresh_charger_status()
        wait = self.charger_protection_wait()
        if self.evse.charger_on:
            if wait > 0:
                self.logger.info(f"Wait {wait} seconds before stop and lower to min charging rate.")
                self.evse = self.emporia.update_charger(self.evse, charge_rate=6)
            else:
                self.evse.charger_on = False
                self.evse = self.emporia.update_charger(self.evse)
                self.last_charging_state_change = datetime.now(tz=self.time_zone)
                self.logger.info(
                    f"Charging stopped and sleep for {self.min_charging_state_change_interval.seconds} seconds!")
                sleep(self.min_charging_state_change_interval.seconds)
        return not self.evse.charger_on

    def refresh_ev_soc(self) -> float:
        if datetime.now(tz=self.time_zone) - self.vehicle_soc_update_time > self.ford.refresh_interval:
            try:
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
        # charge on grid when solar is unavailable
        while datetime.now(tz=self.time_zone) < self.sunrise:
            self.run_charger(self.grid_charge)
        # smart charge on grid or solar during off-peak hours during solar
        while datetime.now(tz=self.time_zone) < self.nem_peak_hour:
            if self.refresh_ev_soc() < self.max_soc_on_grid:
                self.run_charger(self.grid_charge)
            else:
                self.run_charger(self.solar_charge)
        # charge on solar durin peak hours
        while datetime.now(tz=self.time_zone) < self.sunset:
            self.run_charger(self.solar_charge)
        self.logger.info("Stop running at sunset time: %s." % self.sunset)
        self.stop_charger()


if __name__ == "__main__":
    with open("logging_config.json", "r") as f:
        logging.config.dictConfig(json.load(f))

    with open("program.json", "r") as f:
        params = json.load(f)

    home = SolarHome(params=params)
    home.run()
    
