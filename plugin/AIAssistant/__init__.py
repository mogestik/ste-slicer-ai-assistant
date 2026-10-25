# Плагин AI Assistant для STE Slicer (форк Cura 3.6, API плагинов Uranium 5).

from . import AIAssistant


def getMetaData():
    return {}


def register(app):
    return {"extension": AIAssistant.AIAssistant()}
