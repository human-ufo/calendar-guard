import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calguard  # noqa: E402

TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 8, 28, 14, 0, tzinfo=TZ)


def make_event(**kwargs):
    defaults = dict(
        account="cuenta@gmail.com",
        event_id="evt1",
        summary="Entrevista con ACME",
        start=NOW + timedelta(minutes=50),
        all_day=False,
        link="",
    )
    defaults.update(kwargs)
    return calguard.Event(**defaults)


def raw_event(**overrides):
    raw = {
        "id": "abc123",
        "summary": "Entrevista backend",
        "start": {"dateTime": "2026-08-28T15:00:00-03:00", "timeZone": "America/Argentina/Buenos_Aires"},
        "htmlLink": "https://calendar.google.com/event?eid=abc123",
    }
    raw.update(overrides)
    return raw


class ParseDatetimeTest(unittest.TestCase):
    def test_offset_explicito(self):
        dt = calguard.parse_datetime("2026-08-28T15:00:00-03:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=-3))

    def test_sufijo_z(self):
        dt = calguard.parse_datetime("2026-08-28T18:00:00Z")
        self.assertEqual(dt.utcoffset(), timedelta(0))


class ParseEventTest(unittest.TestCase):
    def test_evento_completo_prefiere_hangout_link(self):
        raw = raw_event(hangoutLink="https://meet.google.com/xyz")
        event = calguard.parse_event("cuenta@gmail.com", raw)
        self.assertEqual(event.link, "https://meet.google.com/xyz")
        self.assertEqual(event.summary, "Entrevista backend")
        self.assertFalse(event.all_day)

    def test_fallback_conference_data(self):
        raw = raw_event(conferenceData={"entryPoints": [{"uri": "https://meet.google.com/yyy"}]})
        del raw["htmlLink"]
        event = calguard.parse_event("cuenta@gmail.com", raw)
        self.assertEqual(event.link, "https://meet.google.com/yyy")

    def test_fallback_html_link(self):
        event = calguard.parse_event("cuenta@gmail.com", raw_event())
        self.assertEqual(event.link, "https://calendar.google.com/event?eid=abc123")

    def test_sin_id_devuelve_none(self):
        self.assertIsNone(calguard.parse_event("cuenta@gmail.com", raw_event(id="")))

    def test_sin_start_devuelve_none(self):
        raw = raw_event()
        del raw["start"]
        self.assertIsNone(calguard.parse_event("cuenta@gmail.com", raw))

    def test_dia_completo(self):
        raw = raw_event(start={"date": "2026-08-28"})
        event = calguard.parse_event("cuenta@gmail.com", raw)
        self.assertTrue(event.all_day)
        self.assertIsNotNone(event.start.tzinfo)

    def test_sin_summary(self):
        raw = raw_event()
        del raw["summary"]
        event = calguard.parse_event("cuenta@gmail.com", raw)
        self.assertEqual(event.summary, "(sin título)")


class PickThresholdTest(unittest.TestCase):
    THRESHOLDS = [60, 15]

    def test_sin_cruce(self):
        self.assertIsNone(calguard.pick_threshold(90, self.THRESHOLDS))
        self.assertIsNone(calguard.pick_threshold(61, self.THRESHOLDS))

    def test_cruce_unico(self):
        self.assertEqual(calguard.pick_threshold(60, self.THRESHOLDS), 60)
        self.assertEqual(calguard.pick_threshold(30, self.THRESHOLDS), 60)

    def test_cruce_multiple_toma_el_menor(self):
        self.assertEqual(calguard.pick_threshold(15, self.THRESHOLDS), 15)
        self.assertEqual(calguard.pick_threshold(10, self.THRESHOLDS), 15)
        self.assertEqual(calguard.pick_threshold(0, self.THRESHOLDS), 15)


class SelectAlertsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = calguard.Config()

    def test_evento_en_ventana(self):
        alerts = calguard.select_alerts([make_event()], NOW, self.cfg, set())
        self.assertEqual([(a.event_id, t) for a, t in alerts], [("evt1", 60)])

    def test_arranque_tarde_solo_umbral_actual(self):
        alerts = calguard.select_alerts([make_event(start=NOW + timedelta(minutes=10))], NOW, self.cfg, set())
        self.assertEqual([(a.event_id, t) for a, t in alerts], [("evt1", 15)])

    def test_evento_ya_empezado(self):
        alerts = calguard.select_alerts([make_event(start=NOW - timedelta(minutes=5))], NOW, self.cfg, set())
        self.assertEqual(alerts, [])

    def test_ya_alertado_no_repite(self):
        event = make_event()
        alerted = {f"{event.dedup_key}|60"}
        alerts = calguard.select_alerts([event], NOW, self.cfg, alerted)
        self.assertEqual(alerts, [])

    def test_dia_completo_se_ignora_por_defecto(self):
        event = make_event(all_day=True, start=NOW + timedelta(minutes=30))
        self.assertEqual(calguard.select_alerts([event], NOW, self.cfg, set()), [])
        self.cfg.skip_all_day = False
        self.assertEqual(len(calguard.select_alerts([event], NOW, self.cfg, set())), 1)

    def test_only_keywords(self):
        self.cfg.only_keywords = ["entrevista"]
        pasa = make_event(summary="ENTREVISTA backend")
        no_pasa = make_event(event_id="evt2", summary="Reunión de equipo")
        alerts = calguard.select_alerts([pasa, no_pasa], NOW, self.cfg, set())
        self.assertEqual([a.event_id for a, _ in alerts], ["evt1"])

    def test_skip_keywords(self):
        self.cfg.skip_keywords = ["equipo"]
        evento = make_event(summary="Reunión de equipo")
        self.assertEqual(calguard.select_alerts([evento], NOW, self.cfg, set()), [])

    def test_dedup_key_cambia_con_el_start(self):
        a = make_event()
        b = make_event(start=NOW + timedelta(hours=2))
        self.assertNotEqual(a.dedup_key, b.dedup_key)


class ExtractEventsTest(unittest.TestCase):
    def test_lista(self):
        self.assertEqual(len(calguard.extract_events('[{"id": "1"}, {"id": "2"}]')), 2)

    def test_dict_con_items(self):
        self.assertEqual(len(calguard.extract_events('{"items": [{"id": "1"}]}')), 1)

    def test_json_invalido(self):
        self.assertEqual(calguard.extract_events("no es json"), [])

    def test_vacio(self):
        self.assertEqual(calguard.extract_events("  "), [])


class ParseTokenKeysTest(unittest.TestCase):
    def test_extrae_emails_y_deduplica(self):
        keys = [
            "token:default:a@gmail.com",
            "token:work:b@gmail.com",
            "token:default:a@gmail.com",
            "basura",
            "token:solodospartes",
        ]
        self.assertEqual(calguard.parse_token_keys(keys), ["a@gmail.com", "b@gmail.com"])


class HumanDeltaTest(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(calguard.human_delta(5), "5 min")
        self.assertEqual(calguard.human_delta(59), "59 min")
        self.assertEqual(calguard.human_delta(60), "1 h")
        self.assertEqual(calguard.human_delta(90), "1 h 30 min")


class ConfigLoadTest(unittest.TestCase):
    def test_carga_claves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'window_hours = 12\n'
                'accounts = ["x@gmail.com"]\n'
                'thresholds_minutes = [10, 45]\n'
            )
            cfg = calguard.Config.load(path)
            self.assertEqual(cfg.window_hours, 12)
            self.assertEqual(cfg.accounts, ["x@gmail.com"])
            self.assertEqual(cfg.thresholds_minutes, [45, 10])

    def test_umbrales_invalidos(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("thresholds_minutes = [0]\n")
            with self.assertRaises(ValueError):
                calguard.Config.load(path)

    def test_config_inexistente_usa_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = calguard.Config.load(Path(tmp) / "no-existe.toml")
            self.assertEqual(cfg.thresholds_minutes, [60, 15])


if __name__ == "__main__":
    unittest.main()
