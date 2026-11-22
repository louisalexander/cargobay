from cargobay.mapping import to_remote
from fakes.fake_3dfp import SAMPLE


def test_third_party_item():
    r = to_remote(SAMPLE[0])
    assert r.short_code == "acmePLA01"
    assert r.brand == "Acme"
    assert r.name == "PLA Basic Jet Black"
    assert r.material == "PLA"
    assert r.color_hex == "1A1A1A"
    assert r.location is None  # "Shelf"
    assert r.remaining_grams == 850
    assert r.empty_spool_weight == 200
    assert r.archived is False
    assert r.is_bambu is False
    assert r.bambu_tray_uuid is None
    assert r.filament_key == "9001"


def test_slot_and_shelf_locations():
    assert to_remote(SAMPLE[1]).location == "Printer: Bambu P2S, AMS: 1-2"
    assert to_remote(SAMPLE[2]).location == "Shelf: Office Supply Closet"
    assert to_remote({"short_code": "x", "location": " shelf "}).location is None


def test_empty_is_archived():
    r = to_remote(SAMPLE[3])
    assert r.remaining_grams == 0 and r.archived is True


def test_bambu_reel():
    r = to_remote(SAMPLE[4])
    assert r.is_bambu
    assert r.name == "PLA Basic Bambu Green"
    assert r.bambu_tray_uuid == "0123456789ABCDEF0123456789ABCDEF"


def test_bambu_brand_any_case():
    assert to_remote({"short_code": "x", "brand": "Bambu Lab"}).is_bambu
    assert to_remote({"short_code": "x", "brand": "bambu lab "}).is_bambu


def test_tray_uuid_normalised_and_empty_is_none():
    r = to_remote({"short_code": "x", "spool_properties": {"bambu_tray_uuid": "ab-cd"}})
    assert r.bambu_tray_uuid == "ABCD"
    r = to_remote({"short_code": "x", "spool_properties": {"bambu_tray_uuid": ""}})
    assert r.bambu_tray_uuid is None


def test_bad_rgb_is_none():
    assert to_remote({"short_code": "x", "rgb": "red"}).color_hex is None
    assert to_remote({"short_code": "x", "rgb": "#abc123"}).color_hex == "ABC123"


def test_filament_key_falls_back_to_brand_and_name():
    r = to_remote({"short_code": "x", "brand": "Acme", "material": "PLA", "color": "Red"})
    assert r.filament_key == "Acme|PLA Red"


def test_missing_fields_are_none():
    r = to_remote({"short_code": "x"})
    assert r.short_code == "x"
    assert r.brand == "" and r.material == "" and r.color_name == ""
    assert r.color_hex is None and r.location is None
    assert r.remaining_grams is None and r.empty_spool_weight is None
    assert r.bambu_tray_uuid is None
    assert r.archived is False and r.is_bambu is False
