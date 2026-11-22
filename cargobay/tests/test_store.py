from cargobay.store import Store, normalise_uid


def test_spool_ids_stable_and_never_reused(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    assert s.spool_id("abc") == 1
    assert s.spool_id("def") == 2
    assert s.spool_id("abc") == 1
    assert s.short_code(2) == "def"
    assert s.short_code(99) is None
    s.close()
    s = Store(path)
    assert s.spool_id("ghi") == 3


def test_normalise_uid():
    assert normalise_uid("04:a1:B2 c3-d4") == "04A1B2C3D4"


def test_tag_link_owner_unlink(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.link_tag(1, "04A1", "ndef")
    assert s.tag_owner("04A1") == 1
    assert s.tags(1) == [("04A1", "ndef")]
    assert s.unlink_tag(1, "04A1") is True
    assert s.unlink_tag(1, "04A1") is False
    assert s.tag_owner("04A1") is None


def test_extra_merges_per_key(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.merge_extra(1, {"a": '"1"'})
    s.merge_extra(1, {"b": '"2"'})
    assert s.extra(1) == {"a": '"1"', "b": '"2"'}
    s.merge_extra(1, {"a": '""'})
    assert s.extra(1)["a"] == '""'


def test_bindings_roundtrip_persist(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    s.set_binding(2, 1)
    s.close()
    s = Store(path)
    assert s.bindings() == {2: 1}
    s.clear_binding(2)
    assert s.bindings() == {}


def test_filament_vendor_local_unbound_quota_snapshot(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    assert s.filament_id("f1") == 1
    assert s.filament_id("f1") == 1
    assert s.vendor_id("Bambu") == s.vendor_id("BAMBU")
    assert s.spool_id("x") == 1
    assert s.local(1).spool_weight is None
    s.set_local(1, spool_weight=250.0, last_used="2026-01-01")
    loc = s.local(1)
    assert (loc.spool_weight, loc.initial_weight, loc.last_used) == (250.0, None, "2026-01-01")
    assert s.last_unbound(1) is None
    s.mark_unbound(1, 12.5)
    assert s.last_unbound(1) == 12.5
    q = s.quota()
    assert (q.remaining, q.used, q.warned) == (None, 0, False)
    s.set_quota("2026-11", 900, 100, True)
    q = s.quota()
    assert (q.month, q.remaining, q.used, q.warned) == ("2026-11", 900, 100, True)
    assert s.snapshot() is None and s.snapshot_ts() == 0.0
    s.save_snapshot([{"id": 1}], 5.0)
    assert s.snapshot() == [{"id": 1}] and s.snapshot_ts() == 5.0
