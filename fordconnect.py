"""Fordpass API Library"""
import logging
from datetime import timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

postmanHeaders = {
    "Accept": "*/*",
    "User-Agent": "PostmanRuntime/7.42.0",
    "Accept-Encoding": "gzip, deflate, br",
}

apiHeaders = {
    **postmanHeaders,
    "Content-Type": "application/json",
    "Application-Id": "AFDC085B-377A-4351-B23E-5E1D35FB3700"
}

LOGIN_URL = "https://login.ford.com/4566605f-43a7-400a-946e-89cc9fdb0bd7/B2C_1A_SignInSignUp_en-US/oauth2/v2.0/authorize?ford_application_id=AFDC085B-377A-4351-B23E-5E1D35FB3700&country_code=USA&language_code=en-US&response_type=code&client_id=5fb91578-2ac9-476a-b8b4-e9311eab2982&scope=%205fb91578-2ac9-476a-b8b4-e9311eab2982%20openid&redirect_uri=https://fordconnect.cv.ford.com/oauth/callback"
OAUTH_URL = "https://dah2vb2cprod.b2clogin.com/914d88b1-3523-4bf6-9be4-1b96b4f6f919/oauth2/v2.0/token?p=B2C_1A_signup_signin_common"
API_BASE_URL = "https://api.mps.ford.com/api/fordconnect/v3/"
API_GET_VEHICLES = API_BASE_URL + "vehicles"


class FordConnect:
    # Represents a Ford vehicle, with methods for status and issuing commands

    def __init__(self, params: dict):
        self.logger = logging.getLogger(__name__)

        self.client_id = params["client_id"]
        self.client_secret = params["client_secret"]
        self.username = params["username"]
        self.password = params["password"]
        self.refresh_token = params["refresh_token"]
        self.refresh_interval = timedelta(minutes=params["refresh_interval_mins"])
        self.tokens = None

        adapter = HTTPAdapter(max_retries=Retry(connect=3, backoff_factor=0.5))
        self.session = requests.session()
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def refresh_tokens(self):
        if self.tokens == None:
            # refresh token
            data = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token
            }

            response = self.session.post(url=OAUTH_URL, headers=postmanHeaders, data=data)
            response.raise_for_status()
            self.tokens = response.json()

    def vehicle_ids(self):
        self.refresh_tokens()

        headers = {
            **apiHeaders,
            "Authorization": "Bearer " + self.tokens["access_token"]
        }

        response = self.session.get(url=API_GET_VEHICLES, headers=headers)
        response.raise_for_status()
        return response.json()["vehicles"]

    def vehicle_info(self, vehicle_id: str):
        self.refresh_tokens()

        headers = {
            **apiHeaders,
            "Authorization": "Bearer " + self.tokens["access_token"]
        }

        response = self.session.get(url=API_GET_VEHICLES + "/" + vehicle_id, headers=headers)
        response.raise_for_status()
        return response.json()["vehicle"]
