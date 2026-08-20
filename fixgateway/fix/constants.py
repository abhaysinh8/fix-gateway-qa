"""Constants for the FIX 4.4 order-flow subset exercised by this project."""

from enum import Enum, IntEnum


SOH = "\x01"


class Tag(IntEnum):
    BEGIN_STRING = 8
    BODY_LENGTH = 9
    CHECK_SUM = 10
    CL_ORD_ID = 11
    CUM_QTY = 14
    EXEC_ID = 17
    ORDER_ID = 37
    ORDER_QTY = 38
    ORD_STATUS = 39
    ORD_TYPE = 40
    ORIG_CL_ORD_ID = 41
    PRICE = 44
    SENDER_COMP_ID = 49
    SENDING_TIME = 52
    SIDE = 54
    SYMBOL = 55
    TARGET_COMP_ID = 56
    TIME_IN_FORCE = 59
    TRANSACT_TIME = 60
    AVG_PX = 6
    MSG_SEQ_NUM = 34
    MSG_TYPE = 35
    EXEC_TYPE = 150
    LEAVES_QTY = 151


class MsgType(str, Enum):
    NEW_ORDER_SINGLE = "D"
    ORDER_CANCEL_REQUEST = "F"
    ORDER_CANCEL_REPLACE_REQUEST = "G"
    EXECUTION_REPORT = "8"
    ORDER_CANCEL_REJECT = "9"


class Side(str, Enum):
    BUY = "1"
    SELL = "2"


class OrdType(str, Enum):
    MARKET = "1"
    LIMIT = "2"


class OrdStatus(str, Enum):
    NEW = "0"
    PARTIALLY_FILLED = "1"
    FILLED = "2"
    CANCELED = "4"
    REJECTED = "8"


class ExecType(str, Enum):
    NEW = "0"
    PARTIAL_FILL = "1"
    FILL = "2"
    CANCELED = "4"
    REPLACED = "5"
    REJECTED = "8"


class TimeInForce(str, Enum):
    DAY = "0"
    GOOD_TILL_CANCEL = "1"
    AT_THE_OPENING = "2"
    IMMEDIATE_OR_CANCEL = "3"
    FILL_OR_KILL = "4"
    GOOD_TILL_CROSSING = "5"
    GOOD_TILL_DATE = "6"


STANDARD_HEADER_TAGS = {
    Tag.BEGIN_STRING,
    Tag.BODY_LENGTH,
    Tag.MSG_TYPE,
    Tag.SENDER_COMP_ID,
    Tag.TARGET_COMP_ID,
    Tag.MSG_SEQ_NUM,
    Tag.SENDING_TIME,
    Tag.CHECK_SUM,
}


# This is the deliberately bounded order-gateway profile used by the test harness.
# Optional FIX fields remain legal; only these fields are mandatory here.
REQUIRED_TAGS: dict[MsgType, set[int]] = {
    MsgType.NEW_ORDER_SINGLE: STANDARD_HEADER_TAGS
    | {Tag.CL_ORD_ID, Tag.SYMBOL, Tag.SIDE, Tag.ORDER_QTY, Tag.ORD_TYPE},
    MsgType.ORDER_CANCEL_REQUEST: STANDARD_HEADER_TAGS
    | {Tag.CL_ORD_ID, Tag.ORIG_CL_ORD_ID, Tag.SYMBOL, Tag.SIDE},
    MsgType.ORDER_CANCEL_REPLACE_REQUEST: STANDARD_HEADER_TAGS
    | {
        Tag.CL_ORD_ID,
        Tag.ORIG_CL_ORD_ID,
        Tag.SYMBOL,
        Tag.SIDE,
        Tag.ORDER_QTY,
        Tag.ORD_TYPE,
    },
    MsgType.EXECUTION_REPORT: STANDARD_HEADER_TAGS
    | {
        Tag.ORDER_ID,
        Tag.CL_ORD_ID,
        Tag.EXEC_TYPE,
        Tag.ORD_STATUS,
        Tag.SYMBOL,
        Tag.SIDE,
        Tag.ORDER_QTY,
        Tag.CUM_QTY,
        Tag.LEAVES_QTY,
        Tag.AVG_PX,
    },
    MsgType.ORDER_CANCEL_REJECT: STANDARD_HEADER_TAGS
    | {Tag.ORDER_ID, Tag.CL_ORD_ID, Tag.ORIG_CL_ORD_ID, Tag.ORD_STATUS},
}

