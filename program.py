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
        self.start_time, self.stop_time = self.sunrise_sunset()
        self.excessive_ratio = params.get("excessive_ratio") or 0.98
        self.max_soc_on_grid = params.get("max_soc_on_grid") or 60
        if params.get("nem_version") or 3 == 2:
            self.stop_time = parser.parse("15:00").astimezone(self.time_zone)

        # ford ev
        self.ford = FordConnect(params["ford"])
        self.vehicle_id = None
        self.vehicle_soc = 0
        self.vehicle_soc_update_time = datetime.today().astimezone(self.time_zone) - timedelta(days=1)

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

        self.min_excessive_solar = int(6 * 240 * 1.05)
        self.min_charging_state_change_interval = timedelta(minutes=5)
        self.last_charging_state_change = (
                datetime.now().astimezone(self.time_zone) - self.min_charging_state_change_interval
        )

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
            self.logger.info(
                "Connect to Tesla Powerwall: %s" % self.powerwall.is_connected()
            )
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
                "Available solar: {0:,.0f}w [Solar: {1:,.0f}w; Home: {2:,.0f}w; Battery: {3:,.0f}w]".format(
                    available, solar, home, battery
                )
            )
        return int(available)

    def solar_charge(self):
        evse = self.emporia.get_chargers()[0]
        if evse.icon != "CarConnected":
            self.logger.debug("EV charger is not plugged in: %s" % evse.icon)
            return

        excessive = self.available_solar() + evse.charger_on * evse.charging_rate * 240
        if excessive > self.min_excessive_solar:
            charge_rate = int(max(min(excessive * self.excessive_ratio / 240, 40), 6))
            if self.set_charger(charge_rate):
                self.logger.info("Charging at {0}A with exccessive solar {1:,}w".format(evse.charging_rate, excessive))
        else:
            self.logger.info(
                "Excessive solar is not enough: {0:,d}w, min: {1:,d}w".format(excessive, self.min_excessive_solar))
            self.stop_charger()

    def grid_charge(self):
        evse = self.emporia.get_chargers()[0]
        if evse.icon != "CarConnected":
            self.logger.debug("EV charger is not plugged in: %s" % evse.icon)
            return

        if self.refresh_ev_soc() > self.max_soc_on_grid:
            self.logger.info(
                f"EV SOC is {self.vehicle_soc}%, larger than target {self.max_soc_on_grid}%, stop charging on grid")
            self.stop_charger()
        else:
            if self.set_charger(40):
                self.logger.info("Charge at max rate 40A on grid.")

    def set_charger(self, charge_rate: int) -> bool:
        charge_rate = max(min(charge_rate, 6), 40)
        evse = self.emporia.get_chargers()[0]
        wait = self.charger_protection_wait()
        if not evse.charger_on and wait > 0:
            self.logger.info("Charger protection: wait %d seconds to charge." % wait)
            sleep(wait)
            return False
        if not evse.charger_on or evse.charging_rate != charge_rate:
            if not evse.charger_on:
                self.last_charging_state_change = datetime.now()
            evse.charger_on = True
            evse.charging_rate = charge_rate
            evse.max_charging_rate = 40
            self.emporia.update_charger(evse)
            return True
        else:
            self.logger.debug("No change for charging rate @ %sA" % evse.charging_rate)
            return False

    def charger_protection_wait(self) -> int:
        interval = datetime.now(tz=self.time_zone) - self.last_charging_state_change
        return max(
            0, self.min_charging_state_change_interval.seconds - interval.seconds
        )

    def stop_charger(self):
        evse = self.emporia.get_chargers()[0]
        if not evse.charger_on:
            return
        wait = self.charger_protection_wait()
        if wait > 0:
            self.logger.info(f"Wait {wait} seconds before stop and lower to min charging rate.")
            if evse.charging_rate > 6:
                self.emporia.update_charger(evse, charge_rate=6)
        else:
            evse.charger_on = False
            self.emporia.update_charger(evse)
            self.last_charging_state_change = datetime.now()
            self.logger.info(
                f"Charging stopped and sleep for {self.min_charging_state_change_interval.seconds} seconds!")
            sleep(self.min_charging_state_change_interval.seconds)

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

    def run(self):
        self.login_emporia()
        # charge when solar is unavailable
        while datetime.now(tz=self.time_zone) < self.start_time:
            try:
                self.grid_charge()
            except Exception as e:
                self.logger.exception(e)
                self.login_emporia()
            finally:
                sleep(self.ford.refresh_interval.total_seconds())
        # charge when solar is available (sunrise to sunset)
        while self.start_time < datetime.now(tz=self.time_zone) < self.stop_time:
            try:
                self.solar_charge()
            except Exception as e:
                self.logger.exception(e)
                self.login_emporia()
            finally:
                sleep(15)
        self.logger.info("Stop running at configed time: %s." % self.stop_time)
        self.stop_charger()


if __name__ == "__main__":
    with open("logging_config.json", "r") as f:
        logging.config.dictConfig(json.load(f))

    with open("program.json", "r") as f:
        params = json.load(f)

    home = SolarHome(params=params)
    home.run()
