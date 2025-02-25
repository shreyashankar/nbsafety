# -*- coding: future_annotations -*-
from enum import Enum

from nbsafety.utils import fast


EMIT_EVENT = '_X5ix_NBSAFETY_EVT_EMIT'


class TraceEvent(Enum):
    before_stmt = 'before_stmt'
    after_stmt = 'after_stmt'

    attribute = 'attribute'
    subscript = 'subscript'

    before_complex_symbol = 'before_complex_symbol'
    after_complex_symbol = 'after_complex_symbol'

    before_call = 'before_call'
    after_call = 'after_call'
    argument = 'argument'
    before_return = 'before_return'
    after_return = 'after_return'

    before_literal = 'before_literal'
    after_literal = 'after_literal'
    dict_key = 'dict_key'
    dict_value = 'dict_value'
    list_elt = 'list_elt'
    tuple_elt = 'tuple_elt'

    before_assign_rhs = 'before_assign_rhs'
    after_assign_rhs = 'after_assign_rhs'

    line = 'line'
    call = 'call'
    return_ = 'return'
    exception = 'exception'

    # these are included for completeness but will probably not be used
    c_call = 'c_call'
    c_return = 'c_return'
    c_exception = 'c_exception'

    def __str__(self):
        return self.value

    def to_ast(self):
        return fast.Constant(self.value)
