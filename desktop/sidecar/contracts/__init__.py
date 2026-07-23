from . import v1 as v1
from . import v2 as v2


__all__ = [*v1.__all__, "v1", "v2"]

globals().update({name: getattr(v1, name) for name in v1.__all__})
