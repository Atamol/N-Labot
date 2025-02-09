from dotenv import load_dotenv
load_dotenv()

import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import io
import asyncio

from PIL import Image
import matplotlib.pyplot as plt
from matplotlib.table import Table
from matplotlib.font_manager import FontProperties

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Select, Button
from discord.ext import tasks

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "0")
BUTTON_CH_ID  = int(os.getenv("DISCORD_RSV_BUTTON_CH", "0"))
LOG_CH_ID     = int(os.getenv("DISCORD_RSV_LOG_CH", "0"))

regular_font_path = "/app/fonts/NotoSansCJKjp-Regular.ttf"
bold_font_path    = "/app/fonts/NotoSansCJKjp-Bold.ttf"

# 起動時
async def init_reservations(bot: discord.Client):
    """
    Bot起動時に呼ばれる:
      - コマンドを登録
      - 予約通知タスク開始
      - 過去メッセージ削除
      - 予約表メッセージ新規投稿 (以降は同じメッセージを編集)
    """
    register_reservation_commands(bot.tree, bot)
    ReservationNotifier(bot).start()

    channel = bot.get_channel(BUTTON_CH_ID)
    if channel:
        try:
            async for message in channel.history(limit=10):
                if message.author.id == bot.user.id:
                    await message.delete()
        except Exception as e:
            print("予約表の削除に失敗しました:", e)

    control_view = ReservationControlView()
    bot.control_view = control_view
    bot.add_view(control_view)

    # 予約表メッセージを保持
    bot.reservation_message = None

    # 今月以降の予約を表示
    await update_reservation_message(bot, control_view)

# 予約表の更新
async def update_reservation_message(bot: discord.Client, control_view: discord.ui.View):
    channel = bot.get_channel(BUTTON_CH_ID)
    if channel is None:
        print("ボタン表示用チャンネルが見つかりません。")
        return

    now = datetime.utcnow()
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_of_time = datetime(9999,12,31,23,59,59)
    reservations = reservation_manager.get_reservations_in_range(start_of_month, end_of_time)

    table_data = [["団体名", "日付 (曜日)", "部屋", "時間"]]
    weekdays = {0:" (月) ",1:" (火) ",2:" (水) ",3:" (木) ",4:" (金) ",5:" (土) ",6:" (日) "}

    for res in reservations:
        group = res[2]
        room  = res[3]
        start_dt = datetime.fromisoformat(res[4])
        end_dt   = datetime.fromisoformat(res[5])
        date_str = f"{start_dt.month}月{start_dt.day}日" + weekdays[start_dt.weekday()]
        time_str = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
        table_data.append([group, date_str, room, time_str])

    img_buf = create_table_image_matplotlib(table_data, font_size=14)
    file = discord.File(fp=img_buf, filename="current_month.png")

    if getattr(bot, "reservation_message", None):
        try:
            await bot.reservation_message.edit(
                content="**予約一覧**",
                attachments=[file],
                view=control_view
            )
        except Exception as e:
            print("reservation_message 編集失敗:", e)
    else:
        bot.reservation_message = await channel.send(
            "**予約一覧**",
            file=file,
            view=control_view
        )

###############################################################################
# ReservationManager クラス
###############################################################################
class ReservationManager:
    def __init__(self, db_path="reservations.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.create_table()

    def create_table(self):
        c = self.conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                group_name TEXT,
                room_type TEXT,
                start_datetime TEXT,
                end_datetime TEXT,
                created_at TEXT,
                notified INTEGER DEFAULT 0
            )
        ''')
        self.conn.commit()

    def add_reservation(self, user_id, group_name, room_type, start_datetime, end_datetime):
        c = self.conn.cursor()
        c.execute('''
            INSERT INTO reservations (user_id, group_name, room_type, start_datetime, end_datetime, created_at, notified)
            VALUES (?, ?, ?, ?, ?, ?, 0)
        ''', (user_id, group_name, room_type, start_datetime, end_datetime, datetime.utcnow().isoformat()))
        self.conn.commit()
        return c.lastrowid

    def get_reservations_in_range(self, start_dt: datetime, end_dt: datetime):
        c = self.conn.cursor()
        c.execute('''
            SELECT id, user_id, group_name, room_type, start_datetime, end_datetime, created_at, notified
            FROM reservations
            WHERE datetime(start_datetime) >= ? AND datetime(start_datetime) < ?
            ORDER BY start_datetime ASC
        ''', (start_dt.isoformat(), end_dt.isoformat()))
        return c.fetchall()

    def get_reservation_by_id(self, reservation_id):
        c = self.conn.cursor()
        c.execute('''
            SELECT id, user_id, group_name, room_type, start_datetime, end_datetime, created_at, notified
            FROM reservations
            WHERE id = ?
        ''', (reservation_id,))
        return c.fetchone()

    def delete_reservation(self, reservation_id):
        c = self.conn.cursor()
        c.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))
        self.conn.commit()

    def update_reservation(self, reservation_id, group_name, room_type, start_datetime, end_datetime):
        c = self.conn.cursor()
        c.execute('''
            UPDATE reservations
            SET group_name = ?, room_type = ?, start_datetime = ?, end_datetime = ?
            WHERE id = ?
        ''', (group_name, room_type, start_datetime, end_datetime, reservation_id))
        self.conn.commit()

    def mark_notified(self, reservation_id):
        c = self.conn.cursor()
        c.execute("UPDATE reservations SET notified = 1 WHERE id = ?", (reservation_id,))
        self.conn.commit()

    # 予約の取得
    def get_future_reservations(self, user_id: str = None):
        now = datetime.utcnow().isoformat()
        c = self.conn.cursor()
        if user_id:  # 指定あり -> そのユーザのみ
            c.execute('''
                SELECT id, user_id, group_name, room_type, start_datetime, end_datetime, created_at, notified
                FROM reservations
                WHERE datetime(start_datetime) >= ?
                  AND user_id = ?
                ORDER BY start_datetime ASC
            ''', (now, user_id))
        else:        # 指定なし -> 全員分
            c.execute('''
                SELECT id, user_id, group_name, room_type, start_datetime, end_datetime, created_at, notified
                FROM reservations
                WHERE datetime(start_datetime) >= ?
                ORDER BY start_datetime ASC
            ''', (now,))
        return c.fetchall()

reservation_manager = ReservationManager()

# テーブルイメージ
def create_table_image_matplotlib(table_data, font_size=14, cell_padding=10,
                                  regular_font_path=regular_font_path,
                                  bold_font_path=bold_font_path):
    n_rows = len(table_data)
    n_cols = len(table_data[0])
    cell_width = 150
    cell_height= 30
    width  = n_cols * cell_width + cell_padding*2
    height = n_rows * cell_height + cell_padding*2

    regular_font = FontProperties(fname=regular_font_path, size=font_size)
    bold_font    = FontProperties(fname=bold_font_path,    size=font_size)

    fig, ax = plt.subplots(figsize=(width/100, height/100), dpi=100)
    ax.set_axis_off()
    table = Table(ax, bbox=[0, 0, 1, 1])
    for i, row in enumerate(table_data):
        for j, cell_text in enumerate(row):
            if i != 0 and j==0 and len(cell_text) > 10:
                cell_font = FontProperties(fname=bold_font_path, size=font_size-2)
            else:
                cell_font = bold_font if (i==0 or j==0) else regular_font
            cell = table.add_cell(i, j, width=cell_width, height=cell_height,
                                  text=cell_text, loc="center")
            cell.get_text().set_fontproperties(cell_font)
            cell.get_text().set_ha("center")
            cell.get_text().set_va("center")

    for i in range(n_rows):
        table.add_cell(i, -1, width=0, height=cell_height)
    for j in range(n_cols):
        table.add_cell(-1, j, width=cell_width, height=0)

    ax.add_table(table)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf

# ログの更新
async def update_log_message(client: discord.Client, header_text: str, table_data: list):
    img_buf = create_table_image_matplotlib(table_data, font_size=14)
    file = discord.File(fp=img_buf, filename="log_table.png")
    log_channel = client.get_channel(LOG_CH_ID)
    if log_channel:
        await log_channel.send(content=header_text, file=file)

# 団体名の選択
class OrganizationSelectView(View):
    """団体名を選択する View。「その他」なら団体名を自由入力、それ以外は固定"""
    def __init__(self):
        super().__init__(timeout=60)
        orgs = [
            "IT研究会","Gamma","3DP研究会","ボカロ同好会","にゃんぱす","漫研","VRアート会","その他"
        ]
        options = [discord.SelectOption(label=org, value=org) for org in orgs]
        self.select = Select(
            placeholder="団体名を選択してください",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        org = self.select.values[0]
        if org == "その他":
            modal = ReservationModal(
                mode="create",
                organization=None,
                room_type=None
            )
        else:
            modal = ReservationModal(
                mode="create",
                organization=org,
                room_type="大部屋"
            )
        await interaction.response.send_modal(modal)
        self.stop()

# その他団体
class ReservationModalOther(discord.ui.Modal, title="部屋の予約（団体名自由入力）"):
    def __init__(self, room_type: str = "大部屋"):
        super().__init__()
        self.room_type = room_type

        # 1. 団体名の入力
        self.add_item(
            discord.ui.TextInput(
                label="団体名",
                placeholder="例: 団体名",
                required=True
            )
        )
        # 2. 日付 (MM/DD)
        self.add_item(
            discord.ui.TextInput(
                label="日付 (MM/DD)",
                placeholder="例: 1/10",
                required=True,
                max_length=5
            )
        )
        # 3. 開始時刻 (HH:MM)
        self.add_item(
            discord.ui.TextInput(
                label="開始時刻 (HH:MM)",
                placeholder="例: 14:00",
                required=True
            )
        )
        # 4. 終了時刻 (HH:MM)
        self.add_item(
            discord.ui.TextInput(
                label="終了時刻 (HH:MM)",
                placeholder="例: 16:00",
                required=True
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # 1. 入力値を取得
            year = datetime.now().year
            group = self.children[0].value.strip()
            date_input = self.children[1].value.strip()
            start_time_str = self.children[2].value.strip()
            end_time_str   = self.children[3].value.strip()

            # 2. 日付文字列をパース
            month_str, day_str = date_input.split("/")
            date_str = f"{year}-{month_str.zfill(2)}-{day_str.zfill(2)}"
            start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
            end_dt   = datetime.strptime(f"{date_str} {end_time_str}",   "%Y-%m-%d %H:%M")

            # 3. バリデーション
            if start_dt >= end_dt:
                await interaction.response.send_message("開始時刻は終了時刻より前です。", ephemeral=True)
                return
            if start_dt < datetime.now():
                await interaction.response.send_message("過去の日時には予約できません。", ephemeral=True)
                return

            # 4. 重複チェック
            c = reservation_manager.conn.cursor()
            c.execute("""
                SELECT id FROM reservations
                WHERE datetime(replace(start_datetime, 'T', ' ')) < ?
                  AND datetime(replace(end_datetime, 'T', ' ')) > ?
            """, (
                end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                start_dt.strftime("%Y-%m-%d %H:%M:%S")
            ))
            if c.fetchall():
                await interaction.response.send_message("その時間帯には既に予約があります。", ephemeral=True)
                return

        except Exception as e:
            await interaction.response.send_message(f"入力形式エラー: {e}", ephemeral=True)
            return

        # 5. DBに追加 
        reservation_manager.add_reservation(
            user_id=str(interaction.user.id),
            group_name=group,
            room_type=self.room_type,
            start_datetime=start_dt.isoformat(),
            end_datetime=end_dt.isoformat()
        )

        # 6. ログ送信
        weekdays = {0:" (月) ", 1:" (火) ", 2:" (水) ", 3:" (木) ", 4:" (金) ", 5:" (土) ", 6:" (日) "}
        date_disp = f"{start_dt.month}月{start_dt.day}日" + weekdays[start_dt.weekday()]
        time_disp = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"

        table_data = [
            ["団体名", "日付 (曜日)", "部屋", "時間"],
            [group, date_disp, self.room_type, time_disp]
        ]

        # 7. ログチャンネルに画像投稿
        await update_log_message(interaction.client, "✅ 予約を追加しました", table_data)

        # 8. ユーザーにメッセージを表示し，表を更新
        await interaction.response.send_message("予約を追加しました。", ephemeral=True)
        await update_reservation_message(interaction.client, interaction.client.control_view)

# 予約追加
class ReservationModal(Modal):
    def __init__(
        self,
        mode: str = "create",
        reservation_data=None,
        organization=None,
        room_type=None
    ):
        self.mode             = mode
        self.reservation_data = reservation_data
        self.fixed_org        = organization
        self.fixed_room       = room_type

        if mode == "edit":
            title = "部屋の予約（編集）"
        else:
            title = "部屋の予約（新規）"

        super().__init__(title=title)

        if self.mode == "edit" and self.reservation_data is not None:
            group_name_default = self.reservation_data[2]
            room_type_default  = self.reservation_data[3]
            start_dt = datetime.fromisoformat(self.reservation_data[4])
            end_dt   = datetime.fromisoformat(self.reservation_data[5])

            date_default       = f"{start_dt.month}/{start_dt.day}"
            start_time_default = start_dt.strftime("%H:%M")
            end_time_default   = end_dt.strftime("%H:%M")

            self.add_item(TextInput(label="団体名",  default=group_name_default, required=True))
            self.add_item(TextInput(label="部屋",    default=room_type_default,  required=True))
            self.add_item(TextInput(label="日付 (MM/DD)", default=date_default, required=True, max_length=5))
            self.add_item(TextInput(label="開始時刻 (HH:MM)", default=start_time_default, required=True))
            self.add_item(TextInput(label="終了時刻 (HH:MM)", default=end_time_default,   required=True))

        elif self.mode == "create":
            if self.fixed_org is not None:
                self.add_item(TextInput(label="日付 (MM/DD)",   placeholder="例: 1/10", required=True, max_length=5))
                self.add_item(TextInput(label="開始時刻 (HH:MM)", placeholder="例: 14:00", required=True))
                self.add_item(TextInput(label="終了時刻 (HH:MM)", placeholder="例: 16:00", required=True))
            else:
                self.add_item(TextInput(label="団体名", placeholder="例: 学生会", required=True))
                self.add_item(TextInput(label="日付 (MM/DD)", placeholder="例: 1/10", required=True, max_length=5))
                self.add_item(TextInput(label="開始時刻 (HH:MM)", placeholder="例: 14:00", required=True))
                self.add_item(TextInput(label="終了時刻 (HH:MM)", placeholder="例: 16:00", required=True))
        else:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if self.mode == "edit" and self.reservation_data is not None:
                original_year = datetime.fromisoformat(self.reservation_data[4]).year

                group = self.children[0].value.strip()
                room  = self.children[1].value.strip()

                date_input      = self.children[2].value.strip()
                start_time_str  = self.children[3].value.strip()
                end_time_str    = self.children[4].value.strip()

                month_str, day_str = date_input.split("/")
                date_str = f"{original_year}-{month_str.zfill(2)}-{day_str.zfill(2)}"
                start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
                end_dt   = datetime.strptime(f"{date_str} {end_time_str}",   "%Y-%m-%d %H:%M")

                if start_dt >= end_dt:
                    await interaction.response.send_message("開始時刻は終了時刻より前です。", ephemeral=True)
                    return

                res_id = self.reservation_data[0]
                reservation_manager.update_reservation(
                    res_id,
                    group,
                    room,
                    start_dt.isoformat(),
                    end_dt.isoformat()
                )

                # ✏️ ログ投稿
                weekdays = {0:"(月)",1:"(火)",2:"(水)",3:"(木)",4:"(金)",5:"(土)",6:"(日)"}
                w = weekdays[start_dt.weekday()]
                date_disp = f"{start_dt.month}月{start_dt.day}日 {w}"
                table_data = [
                    ["団体名","日付","部屋","時間"],
                    [group, date_disp, room, f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"]
                ]
                await update_log_message(interaction.client, "✏️ 予約を変更しました", table_data)

                await update_reservation_message(interaction.client, interaction.client.control_view)
                await interaction.response.send_message("予約を更新しました。", ephemeral=True)
                self.stop()
                return

            else:
                if self.fixed_org is not None:
                    date_input     = self.children[0].value.strip()
                    start_time_str = self.children[1].value.strip()
                    end_time_str   = self.children[2].value.strip()

                    group = self.fixed_org
                    room  = self.fixed_room if self.fixed_room else "大部屋"

                else:
                    group = self.children[0].value.strip()
                    room  = "大部屋"
                    date_input     = self.children[1].value.strip()
                    start_time_str = self.children[2].value.strip()
                    end_time_str   = self.children[3].value.strip()

                year = datetime.now().year
                month_str, day_str = date_input.split("/")
                date_str = f"{year}-{month_str.zfill(2)}-{day_str.zfill(2)}"
                start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
                end_dt   = datetime.strptime(f"{date_str} {end_time_str}",   "%Y-%m-%d %H:%M")

                if start_dt >= end_dt:
                    await interaction.response.send_message("開始時刻は終了時刻より前です。", ephemeral=True)
                    return

                reservation_manager.add_reservation(
                    user_id=str(interaction.user.id),
                    group_name=group,
                    room_type=room,
                    start_datetime=start_dt.isoformat(),
                    end_datetime=end_dt.isoformat()
                )

                # ✅ ログ投稿
                weekdays = {0:" (月) ",1:" (火) ",2:" (水) ",3:" (木) ",4:" (金) ",5:" (土) ",6:" (日) "}
                date_disp = f"{start_dt.month}月{start_dt.day}日" + weekdays[start_dt.weekday()]
                time_disp = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
                table_data = [
                    ["団体名","日付 (曜日)","部屋","時間"],
                    [group, date_disp, room, time_disp]
                ]
                await update_log_message(interaction.client, "✅ 予約を追加しました", table_data)

                await update_reservation_message(interaction.client, interaction.client.control_view)
                await interaction.response.send_message("予約を追加しました。", ephemeral=True)
                self.stop()

        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)
            return

# 予約一覧表示・編集・削除
class ModifyReservationView(View):
    def __init__(self, user_id, mode, admin_id=None):
        super().__init__(timeout=60)
        self.user_id  = user_id
        self.mode     = mode
        self.admin_id = admin_id
        self.message_ref = None

        # 管理者 -> 全員分，一般 -> 自分のみ
        if str(user_id) == str(admin_id):
            reservations = reservation_manager.get_future_reservations(user_id=None)
        else:
            reservations = reservation_manager.get_future_reservations(user_id=str(user_id))

        options = []
        for res in reservations:
            start_dt = datetime.fromisoformat(res[4])
            label = f"{res[2]} ({res[3]}) : {start_dt.strftime('%m/%d %H:%M')}"
            if len(label) > 80:
                label = label[:77] + "..."
            options.append(discord.SelectOption(label=label, value=str(res[0])))

        if not options:
            options.append(discord.SelectOption(label="予約がありません", value="none", default=True))

        self.select = Select(
            placeholder="予約を選択してください",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if self.select.values[0] == "none":
            await interaction.response.send_message("予約がありません。", ephemeral=True)
            self.stop()
            return

        self.reservation_id = int(self.select.values[0])
        self.message_ref    = interaction.message

        res_data = reservation_manager.get_reservation_by_id(self.reservation_id)
        if not res_data:
            await interaction.response.send_message("予約が見つかりません。", ephemeral=True)
            self.stop()
            return

        # 所有者チェック（管理者ならOK，一般なら user_id = "予約の所有者"）
        if str(self.user_id) != str(self.admin_id) and str(res_data[1]) != str(self.user_id):
            await interaction.response.send_message("他人の予約を操作できません。", ephemeral=True)
            self.stop()
            return

        if self.mode == "edit":
            modal = ReservationModal(mode="edit", reservation_data=res_data)
            await interaction.response.send_modal(modal)
            self.stop()

        elif self.mode == "delete":
            start_dt = datetime.fromisoformat(res_data[4])
            end_dt   = datetime.fromisoformat(res_data[5])
            weekdays = {0:"(月)",1:"(火)",2:"(水)",3:"(木)",4:"(金)",5:"(土)",6:"(日)"}
            w = weekdays[start_dt.weekday()]

            table_data = [
                ["団体名","日付","部屋","時間"],
                [res_data[2],
                 f"{start_dt.month}月{start_dt.day}日 {w}",
                 res_data[3],
                 f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
                ]
            ]

            # ❌ ログ投稿
            await update_log_message(interaction.client, "❌ 予約を取消しました", table_data)

            reservation_manager.delete_reservation(self.reservation_id)
            await interaction.response.send_message("予約を削除しました。", ephemeral=True)

            try:
                await self.message_ref.edit(view=None)
            except Exception as e:
                print(e)

            # 表を更新
            await update_reservation_message(
                interaction.client,
                interaction.client.control_view
            )
            self.stop()

        self.stop()

# ボタン操作
class ReservationControlView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="予約", style=discord.ButtonStyle.primary, custom_id="btn_reserve")
    async def reserve_button(self, interaction: discord.Interaction, button: Button):
        org_view = OrganizationSelectView()
        await interaction.response.send_message("団体名を選択してください。", view=org_view, ephemeral=True)
    
    @discord.ui.button(label="編集", style=discord.ButtonStyle.secondary, custom_id="btn_edit")
    async def edit_button(self, interaction: discord.Interaction, button: Button):
        # 管理者かどうか判定
        is_admin = (str(interaction.user.id) == str(ADMIN_USER_ID))
        if not is_admin:
            # 一般ユーザー -> 自分の予約
            reservations = reservation_manager.get_future_reservations(user_id=str(interaction.user.id))
            if not reservations:
                await interaction.response.send_message("編集できる予約はありません。", ephemeral=True)
                return

            view = ModifyReservationView(
                user_id=str(interaction.user.id),
                mode="edit",
                admin_id=ADMIN_USER_ID
            )
        else:
            # 管理者 -> 全員の予約
            reservations = reservation_manager.get_future_reservations(user_id=None)
            if not reservations:
                await interaction.response.send_message("編集できる予約はありません。(全体に予約なし)", ephemeral=True)
                return
            view = ModifyReservationView(
                user_id=str(interaction.user.id),
                mode="edit",
                admin_id=ADMIN_USER_ID
            )

        await interaction.response.send_message("編集する予約を選択してください。", view=view, ephemeral=True)

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, custom_id="btn_delete")
    async def delete_button(self, interaction: discord.Interaction, button: Button):
        is_admin = (str(interaction.user.id) == str(ADMIN_USER_ID))
        if not is_admin:
            # 一般ユーザー -> 自分の予約
            reservations = reservation_manager.get_future_reservations(user_id=str(interaction.user.id))
            if not reservations:
                await interaction.response.send_message("削除できる予約はありません。", ephemeral=True)
                return
            view = ModifyReservationView(
                user_id=str(interaction.user.id),
                mode="delete",
                admin_id=ADMIN_USER_ID
            )
        else:
            # 管理者 -> 全員の予約
            reservations = reservation_manager.get_future_reservations(user_id=None)
            if not reservations:
                await interaction.response.send_message("削除できる予約はありません。(全体に予約なし)", ephemeral=True)
                return
            view = ModifyReservationView(
                user_id=str(interaction.user.id),
                mode="delete",
                admin_id=ADMIN_USER_ID
            )

        await interaction.response.send_message("削除する予約を選択してください。", view=view, ephemeral=True)

# 当日スケジュールの通知
class ReservationNotifier:
    def __init__(self, bot: discord.Client):
        self.bot        = bot
        self.channel_id = LOG_CH_ID
        self.task       = tasks.loop(hours=24)(self.send_notifications)

    async def send_notifications(self):
        now = datetime.now()
        target_time = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now > target_time:
            target_time += timedelta(days=1)
        wait_seconds = (target_time - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        today = target_time.date()
        reservations_today = reservation_manager.get_reservations_for_date(today)
        channel = self.bot.get_channel(self.channel_id)
        if channel and reservations_today:
            table_data = [["団体名","部屋","時間"]]
            for res in reservations_today:
                group = res[2]
                room  = res[3]
                sdt   = datetime.fromisoformat(res[4])
                edt   = datetime.fromisoformat(res[5])
                time_str = f"{sdt.strftime('%H:%M')} - {edt.strftime('%H:%M')}"
                table_data.append([group, room, time_str])

            img_buf = create_table_image_matplotlib(table_data, font_size=14)
            file = discord.File(fp=img_buf, filename="today_reservations.png")
            await channel.send("**本日の予約一覧**", file=file)
            for res in reservations_today:
                reservation_manager.mark_notified(res[0])
        else:
            print("ReservationNotifier: 当日の予約はありません。")

    def start(self):
        self.task.start()

# 完了したスケジュールの動削除
async def cleanup_expired_reservations():
    await asyncio.sleep(5)
    while True:
        now = datetime.utcnow()
        c = reservation_manager.conn.cursor()
        c.execute("DELETE FROM reservations WHERE datetime(end_datetime) <= ?", (now.isoformat(),))
        reservation_manager.conn.commit()
        await asyncio.sleep(60)

# 週間スケジュールの通知
async def weekly_schedule_sender(bot: discord.Client):
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now()
        if now.weekday() == 0 and now.hour < 6:
            next_monday = now.replace(hour=6, minute=0, second=0, microsecond=0)
        else:
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            next_monday = (now + timedelta(days=days_until_monday)).replace(hour=6, minute=0, second=0, microsecond=0)

        wait_seconds = (next_monday - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        start_of_week = next_monday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_week   = start_of_week + timedelta(days=7)

        reservations_week = reservation_manager.get_reservations_in_range(start_of_week, end_of_week)
        channel = bot.get_channel(LOG_CH_ID)

        if channel and reservations_week:
            table_data = [["団体名","日付 (曜日)","部屋","時間"]]
            weekdays   = {0:" (月) ",1:" (火) ",2:" (水) ",3:" (木) ",4:" (金) ",5:" (土) ",6:" (日) "}
            for res in reservations_week:
                group = res[2]
                room  = res[3]
                sdt   = datetime.fromisoformat(res[4])
                edt   = datetime.fromisoformat(res[5])
                date_str = f"{sdt.month}月{sdt.day}日" + weekdays[sdt.weekday()]
                time_str = f"{sdt.strftime('%H:%M')} - {edt.strftime('%H:%M')}"
                table_data.append([group, date_str, room, time_str])

            img_buf = create_table_image_matplotlib(table_data, font_size=14)
            file = discord.File(fp=img_buf, filename="weekly_reservations.png")
            await channel.send("**今週の予約スケジュール**", file=file)

# コマンドの登録 （/dump_db, /reset_db）
def register_reservation_commands(tree: app_commands.CommandTree, bot: discord.Client):
    tree.add_command(dump_db_command)
    tree.add_command(reset_db_command)

@app_commands.command(name="dump_db", description="デバッグ用: DBの内容を出力する")
async def dump_db_command(interaction: discord.Interaction):
    c = reservation_manager.conn.cursor()
    c.execute("SELECT * FROM reservations")
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("DBには予約が登録されていません。", ephemeral=True)
        return

    output = "DB:\n"
    for row in rows:
        output += f"{row}\n"

    # 2000文字を超えるならファイル
    if len(output) > 1900:
        file_obj = io.StringIO(output)
        file = discord.File(fp=file_obj, filename="db_dump.txt")
        await interaction.response.send_message("DBの内容が長いため、ファイルを添付します。", file=file)
    else:
        await interaction.response.send_message(f"```\n{output}\n```", ephemeral=True)

# 管理者のみ
@app_commands.command(name="reset_db", description="デバッグ用: DBの全予約を削除する")
async def reset_db_command(interaction: discord.Interaction):
    if str(interaction.user.id) != str(ADMIN_USER_ID):
        await interaction.response.send_message("このコマンドは管理者のみが実行できます。", ephemeral=True)
        return

    c = reservation_manager.conn.cursor()
    c.execute("DELETE FROM reservations")
    reservation_manager.conn.commit()
    await interaction.response.send_message("DBの全予約を削除しました。", ephemeral=True)
