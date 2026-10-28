"""JSON values that keep their numeric identity across the Sim JS boundary."""
import math


MAX_SAFE_NUMBER = 9_007_199_254_740_991


def require_sim_input(value, *, _depth=0):
    """Validate without converting data, hashes, IDs or historical records."""
    if _depth > 64:
        raise ValueError('sim_input_depth_exceeded')
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('sim_input_unicode_invalid') from None
        return
    if type(value) in (int, float):
        if abs(value) > MAX_SAFE_NUMBER or type(value) is float and not math.isfinite(value):
            raise ValueError('sim_input_number_out_of_range')
        return
    if type(value) is list:
        for item in value:
            require_sim_input(item, _depth=_depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError('sim_input_key_invalid')
            require_sim_input(key, _depth=_depth + 1)
            require_sim_input(item, _depth=_depth + 1)
        return
    raise ValueError('sim_input_type_invalid')
