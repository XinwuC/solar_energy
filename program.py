import json
import logging
import logging.config
import os

from datetime import datetime, timedelta
from dateutil import parser, tz
from suntime import Sun
from time import sleep

import pypowerwall
import pyemvue


class SolarHome:
    def __init__(self, params: dict) -> None:
        self.logger = logging.getLogger(__name__)

        # set start and stop time
        self.time_zone = tz.gettz("America/Los_Angeles")
        self.start_time, self.stop_time = self.sunrise_sunset()
        self.excessive_ratio = params.get("excessive_ratio") or 0.98
        if params.get("nem_version") or 3 == 2:
            self.stop_time = parser.parse("15:00").astimezone(self.time_zone)

        # powerwall
        self.powerwall_host = params["powerwall"]["host"],
        self.powerwall_user = params["powerwall"]["user"],
        self.powerwall_password = params["powerwall"]["password"],
        self.powerwall = None

        # emporia
        self.emporia_user = params["emporia"]["user"],
        self.emporia_password = params["emporia"]["password"],
        self.emporia_token_file = "keys.json"
        self.emporia = pyemvue.PyEmVue()
        self.login_emporia(username=str(self.emporia_user), password=str(self.emporia_password))

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

    def login_powerwall(self) -> None:
        if not self.powerwall or not self.powerwall.is_connected():
            self.powerwall = pypowerwall.Powerwall(
                host=self.powerwall_host,
                email=self.powerwall_user,
                password=self.powerwall_password
            )
            self.logger.info(
                "Connect to Tesla Powerwall: %s" % self.powerwall.is_connected()
            )

    def login_emporia(self, username: str, password: str) -> None:
        loggedin = False
        if os.path.exists(self.emporia_token_file):
            try:
                with open(self.emporia_token_file) as f:
                    token = json.load(f)
                    self.emporia.login(
                        id_token=token["id_token"],
                        access_token=token["access_token"],
                        refresh_token=token["refresh_token"],
                        token_storage_file=self.emporia_token_file,
                    )
                loggedin = True
            except Exception as e:
                os.remove(self.emporia_token_file)
                self.logger.exception(e)

        if not loggedin:
            self.emporia.login(
                username=username,
                password=password,
                token_storage_file=self.emporia_token_file,
            )
        self.logger.info("Logged into Emporia EVSE")

    def available_solar(self) -> int:
        self.login_powerwall()
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

    def set_charger(self):
        evse = self.emporia.get_chargers()[0]
        if evse.icon != "CarConnected":
            self.logger.debug("EV charger is not plugged in: %s" % evse.icon)
            return

        excessive = self.available_solar() + evse.charger_on * evse.charging_rate * 240
        if excessive > self.min_excessive_solar:
            charge_rate = int(max(min(excessive * self.excessive_ratio / 240, 40), 6))
            wait = self.charger_protection_wait()
            if not evse.charger_on and wait > 0:
                self.logger.info(
                    "Charger protection: wait %d seconds to charge." % wait
                )
                sleep(wait)
                return
            if not evse.charger_on or evse.charging_rate != charge_rate:
                if not evse.charger_on:
                    self.last_charging_state_change = datetime.now()
                evse.charger_on = True
                evse.charging_rate = charge_rate
                evse.max_charging_rate = 40
                self.logger.info(
                    "Charging at {0}A with exccessive solar {1:,}w".format(
                        evse.charging_rate, excessive
                    )
                )
                self.emporia.update_charger(evse)
            else:
                self.logger.debug(
                    "No change for charging rate @ %sA" % evse.charging_rate
                )
        else:
            self.logger.info(
                "Excessive solar is not enough: {0:,d}w, min: {1:,d}w".format(
                    excessive, self.min_excessive_solar
                )
            )
            self.stop_charger()

    def charger_protection_wait(self) -> int:
        interval = datetime.now() - self.last_charging_state_change
        return max(
            0, self.min_charging_state_change_interval.seconds - interval.seconds
        )

    def stop_charger(self):
        evse = self.emporia.get_chargers()[0]
        if not evse.charger_on:
            return
        wait = self.charger_protection_wait()
        if wait > 0:
            self.logger.info(
                "Wait %s seconds before stop and lower to min charging rate." % wait
            )
            if evse.charging_rate > 6:
                self.emporia.update_charger(evse, charge_rate=6)
        else:
            evse.charger_on = False
            evse = self.emporia.update_charger(evse)
            self.last_charging_state_change = datetime.now()
            self.logger.info(
                "Charging stopped and sleep for %d seconds!"
                % self.min_charging_state_change_interval.seconds
            )
            sleep(self.min_charging_state_change_interval.seconds)

    def run(self):
        now = datetime.now().astimezone(self.time_zone)
        # wait till start time
        while now < self.start_time:
            sleep((self.start_time - now).total_seconds())
        # run
        while self.start_time < now < self.stop_time:
            try:
                self.set_charger()
            except Exception as e:
                self.logger.exception(e)
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
