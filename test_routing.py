#!/usr/bin/env python3
"""
Тесты маршрутизации входящих: реестр живых сессий, опечатка в теге, сообщение
без тега.

Почему это стоит тестов, хотя остальной скилл их не имеет: ошибка здесь не
падает и не видна. Сообщение просто оседает в базе под несуществующим
адресатом, отправитель видит доставку и считает задачу переданной — а узнаёт
о потере через несколько дней. Единственный способ поймать такую регрессию
заранее — проверить ветвление явно.

Запуск:  python3 test_routing.py
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import db  # noqa: E402
import ingest as ingest_mod  # noqa: E402

CHAT = "12345"


def _update(uid, text, date=None):
    """Апдейт от владельца бота — только такие ingest принимает."""
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": int(CHAT)},
            "from": {"id": int(CHAT)},
            "message_id": uid * 10,  # у реального апдейта id сообщения свой
            "text": text,
            "date": date or int(time.time()),
        },
    }


class RoutingTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.patch_db = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patch_db.start()
        self.conn = db.get_conn()
        db.init(self.conn)
        self.sent = []  # исходящие предупреждения

    def tearDown(self):
        self.conn.close()
        self.patch_db.stop()
        os.unlink(self.db_path)

    def run_ingest(self, updates):
        """Прогон ingest с подменёнными сетевыми вызовами."""
        self.sent = []

        def fake_request(token, method, params=None, http_timeout=20):
            if method == "getUpdates":
                # Второй вызов (подтверждение offset) — с offset > 0.
                if (params or {}).get("offset", 0) > 0:
                    return {"ok": True, "result": []}
                return {"ok": True, "result": updates}
            return {"ok": True, "result": {}}

        def fake_send(token, chat_id, text, mode="plain"):
            self.sent.append(text)
            return "plain"

        with mock.patch.object(common, "telegram_request", fake_request), \
             mock.patch.object(common, "send_message", fake_send), \
             mock.patch.object(ingest_mod.common, "telegram_request", fake_request), \
             mock.patch.object(ingest_mod.common, "send_message", fake_send):
            return ingest_mod.ingest(self.conn, "token", CHAT)

    def owner_of(self, uid):
        row = self.conn.execute(
            "SELECT session_id FROM messages WHERE update_id=?", (uid,)
        ).fetchone()
        return row[0] if row else None

    def _document_update(self, uid, caption, name="data.json",
                         mime="application/json"):
        """Апдейт с файлом — json, лог, архив: всё, что не картинка."""
        return {
            "update_id": uid,
            "message": {
                "chat": {"id": int(CHAT)},
                "from": {"id": int(CHAT)},
                "caption": caption,
                "date": int(time.time()),
                "document": {
                    "file_id": f"d{uid}",
                    "file_name": name,
                    "mime_type": mime,
                },
            },
        }

    def _photo_update(self, uid, caption):
        """Апдейт с фотографией — у неё текст лежит в caption."""
        return {
            "update_id": uid,
            "message": {
                "chat": {"id": int(CHAT)},
                "from": {"id": int(CHAT)},
                "caption": caption,
                "date": int(time.time()),
                "photo": [{"file_id": f"f{uid}", "file_size": 100}],
            },
        }

    # --- удержание блокировки ----------------------------------------------

    def test_attachments_downloaded_before_any_write(self):
        """Скачивание вложений обязано идти ДО первой записи в базу.

        download_file ходит в сеть с таймаутом до 30 секунд, а первый INSERT
        открывает транзакцию SQLite. Скачивание внутри цикла записи держало
        общую базу заблокированной всё это время, и watcher'ы соседних сессий
        падали с «database is locked», теряя доставку. Порядок вызовов —
        единственное, что отличает рабочий вариант от сломанного, поэтому он
        и проверяется.
        """
        order = []

        def fake_download(token, file_id, dest_stem, http_timeout=30,
                          file_name=None):
            order.append(f"download:{dest_stem}")
            return f"/tmp/{dest_stem}.jpg"

        real_store = db.store

        def spy_store(conn, uid, *args, **kwargs):
            order.append(f"store:{uid}")
            return real_store(conn, uid, *args, **kwargs)

        db.touch_session(self.conn, "alpha")
        self.conn.commit()

        with mock.patch.object(common, "download_file", fake_download), \
             mock.patch.object(ingest_mod.common, "download_file", fake_download), \
             mock.patch.object(db, "store", spy_store), \
             mock.patch.object(ingest_mod.db, "store", spy_store):
            self.run_ingest([
                self._photo_update(701, "$alpha первая"),
                self._photo_update(702, "$alpha вторая"),
            ])

        downloads = [i for i, step in enumerate(order) if step.startswith("download:")]
        stores = [i for i, step in enumerate(order) if step.startswith("store:")]
        self.assertTrue(downloads and stores, f"ожидались оба вида шагов: {order}")
        self.assertLess(
            max(downloads), min(stores),
            f"скачивание должно завершиться до первой записи, порядок: {order}",
        )

    def test_any_document_is_downloaded_not_just_images(self):
        """Файл любого типа доезжает до сессии.

        Документы брались только с mime_type "image/*", и присланный json
        приходил ПУСТЫМ сообщением: отправитель видел доставку, а работа,
        которую файл должен был разблокировать, стояла.
        """
        saved = {}

        def fake_download(token, file_id, dest_stem, http_timeout=30,
                          file_name=None):
            saved[dest_stem] = file_name
            return f"/tmp/{dest_stem}-{file_name}"

        db.touch_session(self.conn, "alpha")
        self.conn.commit()

        with mock.patch.object(common, "download_file", fake_download), \
             mock.patch.object(ingest_mod.common, "download_file", fake_download):
            self.run_ingest([
                self._document_update(801, "$alpha вот форма"),
            ])

        self.assertEqual(saved, {"801": "data.json"},
                         "json обязан скачаться, как и картинка")
        rows = db.inbox(self.conn, "alpha")
        self.assertEqual(len(rows), 1)
        # inbox отдаёт кортежи (update_id, text, media_path, message_id).
        media_path = rows[0][2]
        self.assertTrue(str(media_path).endswith("data.json"),
                        f"путь к файлу должен доехать: {media_path}")

    def test_media_path_is_filled_in_on_a_later_run(self):
        """Файл, скачанный позже сообщения, дописывается в его строку.

        Сообщение сохраняется на первом проходе, а вложение может доехать
        только на следующем — скачивание не должно стоить нам сообщения,
        поэтому его отсутствие не ошибка. Раньше INSERT OR IGNORE молча
        пропускал такую строку, и файл оставался на диске, а сообщение
        утверждало, что вложения нет.
        """
        db.touch_session(self.conn, "alpha")
        db.store(self.conn, 901, "alpha", "первый проход", 0, 0, None)
        self.conn.commit()

        db.store(self.conn, 901, "alpha", "первый проход", 0, 0, "/tmp/f.json")
        self.conn.commit()

        rows = db.inbox(self.conn, "alpha")
        self.assertEqual(rows[0][2], "/tmp/f.json")

        # Уже заполненный путь не перетирается: первая удачная загрузка и есть
        # та, на которую ссылались в доставке.
        db.store(self.conn, 901, "alpha", "первый проход", 0, 0, "/tmp/other.json")
        self.conn.commit()
        self.assertEqual(db.inbox(self.conn, "alpha")[0][2], "/tmp/f.json")

    def test_attachment_line_names_a_file_a_file(self):
        """Картинку надо открыть, файл — прочитать; подписи разные."""
        self.assertTrue(common.attachment_line("/x/a.jpg").startswith("[image:"))
        self.assertTrue(common.attachment_line("/x/a.json").startswith("[файл:"))

    # --- реестр сессий -----------------------------------------------------

    def test_touch_and_known(self):
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        self.assertEqual(set(db.known_sessions(self.conn)), {"alpha", "beta"})

    def test_unrouted_is_never_registered(self):
        """Служебный ярлык не должен попадать в список настоящих сессий."""
        db.touch_session(self.conn, "unrouted")
        self.conn.commit()
        self.assertEqual(db.known_sessions(self.conn), [])

    def test_stale_session_drops_off(self):
        db.touch_session(self.conn, "old")
        self.conn.execute(
            "UPDATE sessions SET last_seen=? WHERE session_id='old'",
            (int(time.time()) - db.KNOWN_SESSION_AGE_SEC - 10,),
        )
        self.conn.commit()
        self.assertEqual(db.known_sessions(self.conn), [])

    # --- маршрутизация -----------------------------------------------------

    def test_known_tag_routes_and_stays_silent(self):
        db.touch_session(self.conn, "tariffs")
        self.conn.commit()
        self.run_ingest([_update(1, "$tariffs сделай отчёт")])
        self.assertEqual(self.owner_of(1), "tariffs")
        self.assertEqual(self.sent, [], "по нормальному сообщению предупреждать нечего")

    def test_unknown_tag_warns_and_suggests_fix(self):
        db.touch_session(self.conn, "integration")
        db.touch_session(self.conn, "bugs")
        self.conn.commit()
        self.run_ingest([_update(2, "$intergation проверь тесты")])
        # Сообщение не потеряно — лежит под указанным тегом и ждёт свою сессию.
        self.assertEqual(self.owner_of(2), "intergation")
        self.assertEqual(len(self.sent), 1)
        warning = self.sent[0]
        self.assertIn("$intergation", warning)
        self.assertIn("$integration", warning, "должна быть подсказка про опечатку")

    def test_untagged_with_several_sessions_is_unrouted(self):
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        self.run_ingest([_update(3, "почини контрастность карточек")])
        self.assertEqual(self.owner_of(3), "unrouted")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("без тега", self.sent[0])

    def test_untagged_with_single_session_is_delivered(self):
        """Адресат ровно один — двусмысленности нет, доставляем."""
        db.touch_session(self.conn, "solo")
        self.conn.commit()
        self.run_ingest([_update(4, "почини контрастность карточек")])
        self.assertEqual(self.owner_of(4), "solo")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("solo", self.sent[0])

    def test_untagged_with_no_sessions_says_so(self):
        self.run_ingest([_update(5, "кто-нибудь?")])
        self.assertEqual(self.owner_of(5), "unrouted")
        self.assertIn("Активных сессий нет", self.sent[0])

    # --- отсутствие спама --------------------------------------------------

    def test_repeat_ingest_does_not_warn_twice(self):
        """Повторный прогон над теми же апдейтами молчит.

        Это главное свойство: ingest вызывается каждые 15 секунд, и
        предупреждение, привязанное к самому сообщению, а не к факту его
        новизны, превратило бы помощь в поток спама.
        """
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        updates = [_update(6, "$typo текст")]
        self.run_ingest(updates)
        self.assertEqual(len(self.sent), 1)
        self.run_ingest(updates)  # те же самые
        self.assertEqual(self.sent, [], "повторных предупреждений быть не должно")

    def test_several_messages_one_typo_give_one_warning(self):
        db.touch_session(self.conn, "integration")
        self.conn.commit()
        self.run_ingest([
            _update(7, "$intergation раз"),
            _update(8, "$intergation два"),
            _update(9, "$intergation три"),
        ])
        self.assertEqual(len(self.sent), 1, "одна опечатка — одно предупреждение")

    # --- совместимость -----------------------------------------------------

    def test_album_tail_still_inherits_owner(self):
        """Хвост альбома приходит без подписи и наследует владельца.

        Проверяем, что новая ветка «нет тега» не сломала старое поведение:
        иначе каждая вторая картинка альбома уезжала бы в unrouted.
        """
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        first = _update(10, "$alpha вот скриншоты")
        first["message"]["media_group_id"] = "grp1"
        tail = _update(11, "")
        tail["message"]["media_group_id"] = "grp1"
        self.run_ingest([first, tail])
        self.assertEqual(self.owner_of(10), "alpha")
        self.assertEqual(self.owner_of(11), "alpha", "хвост альбома идёт к владельцу")
        self.assertEqual(self.sent, [], "альбом — не повод для предупреждения")

    # --- конкуренция за общую базу ----------------------------------------

    def test_uncommitted_touch_blocks_other_sessions(self):
        """Отметка в реестре обязана коммититься сразу.

        Регрессия из реальной работы: watch.py вызывал touch_session и
        коммитил только в конце круга — то есть write-транзакция висела всё
        время сетевого getUpdates внутри ingest (до 20 секунд). Параллельные
        сессии в это окно получали «database is locked».

        Тест фиксирует оба состояния: незакоммиченная отметка блокирует, а
        закоммиченная — нет.
        """
        other = sqlite3.connect(self.db_path, timeout=0)
        other.execute("PRAGMA busy_timeout=100")  # не ждать долго, нам нужен факт
        try:
            db.touch_session(self.conn, "writer")  # транзакция открыта, не закрыта
            with self.assertRaises(sqlite3.OperationalError):
                other.execute(
                    "INSERT INTO sessions(session_id, last_seen) VALUES('rival', 1)"
                )
                other.commit()

            self.conn.commit()  # <- то, чего не хватало в watch.py
            other.execute(
                "INSERT INTO sessions(session_id, last_seen) VALUES('rival', 1)"
            )
            other.commit()  # теперь проходит
        finally:
            other.close()

    def test_foreign_chat_is_ignored(self):
        """Чужие сообщения не попадают в базу и не порождают предупреждений."""
        db.touch_session(self.conn, "alpha")
        self.conn.commit()
        alien = _update(12, "привет")
        alien["message"]["from"] = {"id": 999}
        self.run_ingest([alien])
        self.assertIsNone(self.owner_of(12))
        self.assertEqual(self.sent, [])


    # --- длинные сообщения и наследование адресата -------------------------

    def test_split_message_tail_reaches_the_same_session(self):
        """Хвост сообщения, разрезанного клиентом, идёт тому же адресату.

        Telegram-клиент режет текст длиннее 4096 символов на несколько
        сообщений и ставит тег только в первое. На живой базе это выглядело
        как 4077 + 3805 символов с одним и тем же tg_date, где вторая половина
        оседала в unrouted и не доходила ни до кого. Ловится только здесь:
        отправитель видит оба сообщения доставленными.
        """
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        stamp = int(time.time())
        head = "$alpha " + "x" * 4070
        self.run_ingest([
            _update(801, head, date=stamp),
            _update(802, "y" * 3800, date=stamp),
        ])
        self.assertEqual(self.owner_of(801), "alpha")
        self.assertEqual(self.owner_of(802), "alpha")

    def test_short_previous_is_not_treated_as_split(self):
        """Короткое предыдущее — это не разрезанный текст, а просто сообщение.

        Разделять важно: склейка опирается на длину, и без этой проверки любое
        сообщение, отправленное следом, считалось бы продолжением.
        """
        db.touch_session(self.conn, "alpha")
        self.conn.commit()
        stamp = int(time.time())
        self.run_ingest([_update(811, "$alpha коротко", date=stamp)])
        self.assertIsNone(
            db.continuation_owner(self.conn, 812, stamp),
            "короткое сообщение не должно выглядеть как разрезанное",
        )

    def test_untagged_right_after_tagged_inherits_owner(self):
        """Картинка вдогонку без тега уходит туда же, куда шло сообщение перед ней."""
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        stamp = int(time.time())
        self.run_ingest([
            _update(821, "$alpha посмотри скрин", date=stamp),
            _update(822, "вот он", date=stamp + 5),
        ])
        self.assertEqual(self.owner_of(822), "alpha")
        self.assertTrue(any("alpha" in s for s in self.sent),
                        f"о догадке нужно сообщить в чат: {self.sent}")

    def test_inheritance_expires(self):
        """Спустя окно адресат не наследуется — иначе догадка станет наглой."""
        db.touch_session(self.conn, "alpha")
        db.touch_session(self.conn, "beta")
        self.conn.commit()
        stamp = int(time.time())
        self.run_ingest([_update(831, "$alpha давняя задача", date=stamp)])
        self.run_ingest([
            _update(831, "$alpha давняя задача", date=stamp),
            _update(832, "не связано", date=stamp + db.RECENT_OWNER_WINDOW_SEC + 60),
        ])
        self.assertEqual(self.owner_of(832), "unrouted")

    def test_inheritance_only_from_a_live_session(self):
        """Наследовать адрес мёртвой сессии — та же потеря, только молча."""
        db.touch_session(self.conn, "beta")
        db.touch_session(self.conn, "gamma")
        self.conn.commit()
        stamp = int(time.time())
        self.run_ingest([
            _update(841, "$dead уже не работает", date=stamp),
            _update(842, "продолжение мысли", date=stamp + 3),
        ])
        self.assertEqual(self.owner_of(842), "unrouted")

    # --- длина исходящих ----------------------------------------------------

    def test_split_for_send_keeps_every_chunk_within_limit(self):
        """Куски укладываются в лимит и вместе дают исходный текст.

        Проверено живым API: sendMessage отклоняет 5011 символов с
        «message is too long», то есть длинный отчёт не приходил укороченным —
        он не приходил вовсе.
        """
        text = "\n".join(f"строка {i} " + "z" * 100 for i in range(200))
        chunks = common.split_for_send(text, common.PLAIN_LIMIT)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), common.PLAIN_LIMIT)
        self.assertEqual("\n".join(chunks), text)

    def test_split_for_send_handles_one_endless_line(self):
        """Строка без переносов длиннее лимита режется по символам, а не роняет отправку."""
        chunks = common.split_for_send("q" * 10000, common.PLAIN_LIMIT)
        self.assertEqual("".join(chunks), "q" * 10000)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), common.PLAIN_LIMIT)

    def test_long_plain_message_goes_out_in_parts(self):
        calls = []

        def fake_request(token, method, params=None, http_timeout=20):
            calls.append((method, params))
            return {"ok": True, "result": {}}

        with mock.patch.object(common, "telegram_request", fake_request):
            common.send_message("t", CHAT, "w" * 9000, mode="plain")
        self.assertEqual(len(calls), 3, f"9000 символов = три сообщения: {len(calls)}")
        for _method, params in calls:
            self.assertLessEqual(len(params["text"]), common.PLAIN_LIMIT)

    def test_rich_rejection_recuts_chunk_for_html_limit(self):
        """Кусок, законный для rich, при откате на html обязан быть перерезан.

        Лимиты различаются впятеро (20000 против 4096), поэтому простой
        перепосыл того же куска другим методом снова упёрся бы в «too long» —
        и деградация формата, задуманная как страховка, теряла бы сообщение.
        """
        sent = []

        def fake_request(token, method, params=None, http_timeout=20):
            if method == "sendRichMessage":
                raise RuntimeError("метод недоступен")
            sent.append(params["text"])
            return {"ok": True, "result": {}}

        with mock.patch.object(common, "telegram_request", fake_request):
            common.send_message("t", CHAT, "e" * 12000, mode="rich")
        self.assertTrue(sent)
        for text in sent:
            self.assertLessEqual(len(text), common.PLAIN_LIMIT)
        self.assertEqual(len("".join(sent)), 12000)


    def test_russian_layout_tag_is_repaired(self):
        """$Ы6 — это $S6, набранное не в той раскладке.

        В живом инбоксе такое сообщение («$Ы6 оплата прошла») пролежало
        недоставленным: старая регулярка кириллицу не видела вовсе, поэтому
        оно считалось безадресным.
        """
        db.touch_session(self.conn, "S6")
        db.touch_session(self.conn, "T6")
        self.conn.commit()
        self.run_ingest([_update(851, "$\u042b6 оплата прошла")])
        self.assertEqual(self.owner_of(851), "S6")
        row = self.conn.execute(
            "SELECT text FROM messages WHERE update_id=851").fetchone()
        self.assertEqual(row[0], "оплата прошла")

    def test_russian_word_after_dollar_is_not_a_tag(self):
        """«$Проверь …» — не адрес: перевод раскладки ни с чем не совпал."""
        db.touch_session(self.conn, "S6")
        db.touch_session(self.conn, "T6")
        self.conn.commit()
        self.run_ingest([_update(861, "$Проверь очередь платежей")])
        self.assertEqual(self.owner_of(861), "unrouted")

class ReactionTest(unittest.TestCase):
    """Реакции-статусы на сообщениях владельца (👀 → ✍ → 👍).

    Своя фикстура, а не наследование от RoutingTest: наследник прогнал бы весь
    его набор ещё раз, и число тестов росло бы при каждом новом классе.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.patch_db = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patch_db.start()
        self.conn = db.get_conn()
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.patch_db.stop()
        os.unlink(self.db_path)

    def run_ingest(self, updates):
        def fake_request(token, method, params=None, http_timeout=20):
            if method == "getUpdates":
                if (params or {}).get("offset", 0) > 0:
                    return {"ok": True, "result": []}
                return {"ok": True, "result": updates}
            return {"ok": True, "result": {}}

        with mock.patch.object(common, "telegram_request", fake_request), \
             mock.patch.object(ingest_mod.common, "telegram_request", fake_request), \
             mock.patch.object(common, "send_message", lambda *a, **k: "plain"), \
             mock.patch.object(ingest_mod.common, "send_message", lambda *a, **k: "plain"):
            return ingest_mod.ingest(self.conn, "token", CHAT)

    def test_message_id_is_stored(self):
        """Без message_id реакцию ставить не на что — он обязан сохраняться."""
        db.touch_session(self.conn, "alpha")
        self.conn.commit()
        self.run_ingest([_update(1, "задача $alpha")])

        row = self.conn.execute(
            "SELECT message_id FROM messages WHERE update_id=1"
        ).fetchone()
        self.assertEqual(row[0], 10)

    def test_inbox_returns_message_id(self):
        """inbox отдаёт message_id — иначе доставка не сможет пометить ✍."""
        db.touch_session(self.conn, "alpha")
        self.conn.commit()
        self.run_ingest([_update(2, "вопрос $alpha")])

        rows = db.inbox(self.conn, "alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 4, "форма строки: update_id, text, media, message_id")
        self.assertEqual(rows[0][3], 20)

    def test_close_in_progress_returns_ids_to_mark_done(self):
        """notify --done ставит 👍 по этим id, поэтому их надо ВЕРНУТЬ."""
        db.touch_session(self.conn, "alpha")
        self.conn.commit()
        self.run_ingest([_update(3, "работа $alpha")])
        db.mark(self.conn, 3, "in_progress")
        self.conn.commit()

        closed, message_ids = db.close_in_progress(self.conn, "alpha")
        self.assertEqual(closed, 1)
        self.assertEqual(message_ids, [30])

    def test_close_in_progress_skips_rows_without_message_id(self):
        """Строки, записанные до миграции, id не имеют — их нужно пропустить,
        иначе в API уйдёт None и запрос упадёт."""
        self.conn.execute(
            "INSERT INTO messages(update_id, session_id, text, tg_date, received_at,"
            " status) VALUES(4,'alpha','старая',0,0,'in_progress')"
        )
        self.conn.commit()

        closed, message_ids = db.close_in_progress(self.conn, "alpha")
        self.assertEqual(closed, 1)
        self.assertEqual(message_ids, [])

    def test_reaction_is_skipped_without_message_id(self):
        """set_reaction на пустом id не должен ходить в сеть."""
        calls = []

        def fake_request(token, method, params=None, http_timeout=20):
            calls.append(method)
            return {"ok": True, "result": {}}

        with mock.patch.object(common, "telegram_request", fake_request):
            self.assertFalse(common.set_reaction("t", CHAT, None, common.REACTION_READ))
            self.assertEqual(calls, [])

            self.assertTrue(common.set_reaction("t", CHAT, 42, common.REACTION_READ))
            self.assertEqual(calls, ["setMessageReaction"])

    def test_reaction_failure_is_swallowed(self):
        """Отказ API по реакции не должен ронять доставку."""
        def boom(token, method, params=None, http_timeout=20):
            raise RuntimeError("REACTION_INVALID")

        with mock.patch.object(common, "telegram_request", boom):
            self.assertFalse(common.set_reaction("t", CHAT, 42, "\u2705"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
