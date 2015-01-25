from __future__ import unicode_literals

from collections import defaultdict


def convert_value(s):
    if s.lower() == 'none':
        return None
    elif s.lower() == 'true':
        return True
    elif s.lower() == 'false':
        return False
    return s


class UnexpectedSettingName(ValueError):
    pass


class SettingsManager(object):
    def __init__(self):
        self._settings = {}
        self._initializeSettings()
        self._stacks = defaultdict(list)

    def _initializeSettings(self):
        raise NotImplementedError

    def setting(self, name):
        return self._settings[name]

    def setSetting(self, name, value):
        if name not in self._settings:
            raise UnexpectedSettingName(name)
        self._settings[name] = value

    def pushSetting(self, name, value):
        self._stacks[name].append(self.setting(name))
        self.setSetting(name, value)

    def popSetting(self, name):
        self.setSetting(name, self._stacks[name].pop())

    def updateSettings(self, new_settings):
        """Update the settings with a selective merge or a complete overwrite."""
        for key, value in new_settings.items():
            self.setSetting(key, value)

    def updateSettingsFromConfigStr(self, config_str):
        values = [line.split('=', 1) for line in config_str.strip().splitlines()]
        settings = dict(
            (key.strip(), convert_value(value.strip()))
            for key, value in values
        )
        self.updateSettings(settings)
