from django.apps import AppConfig



class CaptureQueriesAppConfig(AppConfig):
    name = "capquery"

    def ready(self):
        print("Ok")
        1/0