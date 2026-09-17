"""Compiled application configuration.

``config.app_config`` replaced ``config/app_config.json`` (#546): the shipped
values are Python literals that compile into the executable with the rest
of the code, so a packaged build carries no editable configuration file.
``config.tec_params`` did the same for ``tec_params.json``.
"""
