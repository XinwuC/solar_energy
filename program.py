import json
import logging
import logging.config
import os
from time import sleep
import pypowerwall
import pyemvue


class SolarHome:
    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.evse_token_file = "keys.json"

    def login_powerwall(self, host: str, email: str, password: str) -> None:
        self.powerwall = pypowerwall.Powerwall(
            host=host, email=email, password=password, timezone="America/Los_Angeles"
        )
        self.logger.info(
            "Connect to Tesla Powerwall: %s" % self.powerwall.is_connected()
        )

    def login_emporia(self, username: str, password: str) -> None:
        self.emporia = pyemvue.PyEmVue()
        if os.path.exists(self.evse_token_file):
            with open(self.evse_token_file) as f:
                token = json.load(f)
                self.emporia.login(
                    id_token=token["id_token"],
                    access_token=token["access_token"],
                    refresh_token=token["refresh_token"],
                    token_storage_file=self.evse_token_file,
                )
        else:
            self.emporia.login(
                username=username,
                password=password,
                token_storage_file=self.evse_token_file,
            )
        self.logger.info("Logged into Emporia EVSE")
        self.evse = self.emporia.get_chargers()[0]

    def available_solar(self) -> int:
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
        if self.evse.icon != "CarConnected":
            self.logger.debug("EV charger is not plugged in.")
            return

        excessive = self.available_solar()
        if excessive * 0.95 > 240 * 6:
            charge_rate = max(min(excessive * 0.95 / 240, 40), 6)
            self.evse.charger_on = True
            self.evse.charging_rate = charge_rate
            self.evse.max_charging_rate = 40
            self.evse = self.emporia.update_charger(self.evse)
            self.logger.debug("Charging at %dA" % self.evse.charging_rate)
        else:
            self.logger.debug(
                "Excessive solar is not enough: {0:,d}w".format(excessive)
            )
            self.stop_charger()

    def stop_charger(self):
        self.evse.charger_on = False
        self.evse = self.emporia.update_charger(self.evse)
        self.logger.debug("Charging stopped!")

    def run(self):
        while True:
            self.set_charger()
            sleep(60)

    def __del__(self):
        if self.evse:
            self.stop_charger()


if __name__ == "__main__":
    with open("logging_config.json", "r") as f:
        logging.config.dictConfig(json.load(f))

    home = SolarHome()
    home.login_powerwall(
        host="", email="", password=""
    )
    home.login_emporia(username="", password="")
    home.run()
