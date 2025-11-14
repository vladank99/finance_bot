# tg_finance_bot_minimal_loop.py
# -*- coding: utf-8 -*-

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple

from dotenv import load_dotenv
import gspread
from gspread.utils import rowcol_to_a1  # только rowcol_to_a1
from google.oauth2.service_account import Credentials
import json
from googleapiclient.discovery import build

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========= UTIL: number column -> 'A1' column =========
def col_to_a1(col: int) -> str:
    """1 -> 'A', 2 -> 'B', 27 -> 'AA', ..."""
    if col < 1:
        raise ValueError("Column index must be >= 1")
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s

# ================== ENV & GOOGLE ==================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if service_account_json:
    # прод: берём JSON из переменной окружения (Render)
    info = json.loads(service_account_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
else:
    # локально: читаем файл
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)

gc = gspread.authorize(creds)
sheets_service = build("sheets", "v4", credentials=creds)


# ================== UI CONSTS ==================
ADD_AMOUNT, ADD_DESC = range(2)

HEADER_SELF = "Траты на себя"

BTN_ADD = "➕ Добавить"
BTN_DONE_CB = "done"

# Главная клавиатура — только одна кнопка
MAIN_KB = ReplyKeyboardMarkup([[KeyboardButton(BTN_ADD)]], resize_keyboard=True)

RUS_MONTHS = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

# ================== DATA ==================
@dataclass
class BlockRange:
    ws_title: str
    start_row: int
    end_row: int
    cat_col: int
    amount_col: int
    sheet_id: int

_WS_CACHE: dict[str, BlockRange] = {}

# ================== SHEETS HELPERS ==================
def month_sheet_title(dt: datetime) -> str:
    return f"{RUS_MONTHS[dt.month]} {dt.year}"

def open_month_ws(dt: datetime):
    sh = gc.open_by_key(SPREADSHEET_ID)
    title = month_sheet_title(dt)
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=300, cols=12)

def _get_sheet_id(ws) -> int:
    return ws._properties["sheetId"]

def _find_self_block(ws) -> BlockRange:
    cache_key = f"{ws.spreadsheet.id}:{ws.title}"
    if cache_key in _WS_CACHE:
        return _WS_CACHE[cache_key]

    values = ws.get_all_values()
    header_row, header_col = None, None
    for r, row in enumerate(values, start=1):
        for c, v in enumerate(row, start=1):
            if (v or "").strip() == HEADER_SELF:
                header_row, header_col = r, c
    if not header_row:
        raise RuntimeError("Не найден заголовок 'Траты на себя'.")

    cat_col = header_col
    amount_col = header_col + 1
    start = header_row + 2

    max_rows = max(ws.row_count, start + 1000)
    a1_range = f"{col_to_a1(cat_col)}{start}:{col_to_a1(amount_col)}{max_rows}"
    block_values = ws.get(a1_range)

    last_nonempty = start - 1
    for i, row in enumerate(block_values, start=start):
        v1 = (row[0] if len(row) > 0 else "").strip()
        v2 = (row[1] if len(row) > 1 else "").strip()
        if v1 or v2:
            last_nonempty = i
        else:
            break

    br = BlockRange(
        ws_title=ws.title,
        start_row=start,
        end_row=last_nonempty,
        cat_col=cat_col,
        amount_col=amount_col,
        sheet_id=_get_sheet_id(ws),
    )
    _WS_CACHE[cache_key] = br
    return br

def _next_insert_row(ws, br: BlockRange) -> int:
    first = max(br.start_row, br.end_row + 1)
    a1_range = f"{col_to_a1(br.cat_col)}{first}:{col_to_a1(br.amount_col)}{ws.row_count}"
    rng = ws.get(a1_range)
    if not rng:
        return first
    for idx, row in enumerate(rng, start=first):
        v1 = (row[0] if len(row) > 0 else "").strip()
        v2 = (row[1] if len(row) > 1 else "").strip()
        if not v1 and not v2:
            return idx
    ws.add_rows(100)
    return ws.row_count - 99

def _copy_format_and_write(
    ws_id: str,
    sheet_id: int,
    src_row: int,
    dst_row: int,
    cat_col: int,
    amount_col: int,
    what: str,
    amount: float,
    note_text: str,
):
    """Копируем формат последней строки блока и записываем значения+note одной батч-операцией."""
    start_col = min(cat_col, amount_col) - 1
    end_col = max(cat_col, amount_col)

    update_cells_request = {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": dst_row - 1,
                "endRowIndex": dst_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "rows": [
                {
                    "values": [
                        {"userEnteredValue": {"stringValue": what}, "note": note_text},
                        {"userEnteredValue": {"numberValue": float(amount)}},
                    ]
                }
            ],
            "fields": "userEnteredValue,note",
        }
    }

    body = {
        "requests": [
            {
                "copyPaste": {
                    "source": {
                        "sheetId": sheet_id,
                        "startRowIndex": src_row - 1,
                        "endRowIndex": src_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    },
                    "destination": {
                        "sheetId": sheet_id,
                        "startRowIndex": dst_row - 1,
                        "endRowIndex": dst_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    },
                    "pasteType": "PASTE_FORMAT",
                }
            },
            update_cells_request,
        ]
    }
    sheets_service.spreadsheets().batchUpdate(spreadsheetId=ws_id, body=body).execute()

def add_record(amount: float, what: str, dt: datetime) -> Tuple[str, int]:
    ws = open_month_ws(dt)
    br = _find_self_block(ws)
    row = _next_insert_row(ws, br)

    src_row = max(br.end_row, br.start_row)
    rec_id = uuid.uuid4().hex[:8]
    note_text = f"id={rec_id}; ts={dt.isoformat()}"

    _copy_format_and_write(
        ws_id=ws.spreadsheet.id,
        sheet_id=br.sheet_id,
        src_row=src_row,
        dst_row=row,
        cat_col=br.cat_col,
        amount_col=br.amount_col,
        what=what,
        amount=amount,
        note_text=note_text,
    )

    br.end_row = max(br.end_row, row)
    _WS_CACHE[f"{ws.spreadsheet.id}:{ws.title}"] = br
    return rec_id, row

# ================== UI HELPERS ==================
def _done_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово", callback_data=BTN_DONE_CB)]])

async def show_main(update: Update, text: str):
    if update.message:
        await update.message.reply_text(text, reply_markup=MAIN_KB)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text)
        await update.callback_query.message.reply_text("Нажми «➕ Добавить», чтобы внести трату.", reply_markup=MAIN_KB)

# ================== HANDЛERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main(update, "Привет! Нажми «➕ Добавить», чтобы внести трату.")

# --- Добавление расхода ---
async def add_entry_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи сумму (например 250.50)")
    return ADD_AMOUNT

async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().replace(",", ".")
    try:
        amount = float(txt)
    except ValueError:
        await update.message.reply_text("Не понял сумму. Попробуй ещё раз, например 199.99")
        return ADD_AMOUNT
    context.user_data["amount"] = amount
    # Здесь НЕТ «Готово»: просто спрашиваем «на что?»
    await update.message.reply_text("Окей. Теперь напиши — *на что?*", parse_mode="Markdown")
    return ADD_DESC

async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    what = update.message.text.strip()
    amount = context.user_data.pop("amount")
    rec_id, _row = add_record(amount, what, datetime.now())

    # 1) подтверждаем и даём кнопку «Готово»
    await update.message.reply_text(
        f"Готово! Добавил *{what}* на *{amount:.2f}*. ID: `{rec_id}`",
        parse_mode="Markdown",
        reply_markup=_done_inline(),
    )
    # 2) сразу запускаем новый цикл без лишних кнопок
    await update.message.reply_text("Ок, добавим ещё. Введи сумму:")
    return ADD_AMOUNT

async def done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Супер, заходи ещё! 👋")
    await q.message.reply_text("Чтобы добавить новую трату позже — нажми «➕ Добавить».", reply_markup=MAIN_KB)
    return ConversationHandler.END

# Вне диалога: если прислали число — стартуем цикл; иначе подсказываем
async def free_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == BTN_ADD:
        return await add_entry_start(update, context)
    txt = update.message.text.strip().replace(",", ".")
    try:
        amount = float(txt)
        context.user_data["amount"] = amount
        await update.message.reply_text("Принял. Теперь — на что?")
        return ADD_DESC
    except ValueError:
        await update.message.reply_text("Не понял. Нажми «➕ Добавить» или просто введи сумму.", reply_markup=MAIN_KB)

# ================== APP ==================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex(f"^{BTN_ADD}$"), add_entry_start)],
        states={
            ADD_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount),
                CallbackQueryHandler(done_cb, pattern=f"^{BTN_DONE_CB}$"),
            ],
            ADD_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc),
                CallbackQueryHandler(done_cb, pattern=f"^{BTN_DONE_CB}$"),
            ],
        },
        fallbacks=[],   # других кнопок больше нет
        name="add_conv",
        persistent=False,
        allow_reentry=True,
    )

    app.add_handler(MessageHandler(filters.COMMAND, start))  # /start на всякий
    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(done_cb, pattern=f"^{BTN_DONE_CB}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_router))
    return app

def main():
    app = build_app()
    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()



