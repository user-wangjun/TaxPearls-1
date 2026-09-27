"""Shared input validation error, independent of individual material parsers."""


class InputError(Exception):
    """输入材料不符合模板要求。"""
