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

        def fake_download(token, file_id, dest_stem, http_timeout=30):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
