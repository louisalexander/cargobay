import dataclasses

from cargobay.mapping import Remote
from cargobay.profiles import Profiles, SetFilament, load_table

TABLE = load_table()


def remote(material="PLA", name="PLA Basic Jet Black", brand="Acme", color_hex="1A1A1A"):
    return Remote(short_code="x", brand=brand, name=name, material=material,
                  color_name="", color_hex=color_hex, location=None, remaining_grams=500,
                  empty_spool_weight=None, bambu_tray_uuid=None, archived=False,
                  filament_key="k", is_bambu=False)


def test_generated_table_has_generics():
    for idx in ("GFL99", "GFG99", "GFB99", "GFB98", "GFC99"):
        assert idx in TABLE
    assert all(e["name"].startswith("Generic") for e in TABLE.values())


def test_material_lookup():
    p = Profiles(TABLE, [{"filament": "acme silk red ", "tray_info_idx": "GFL96"}])
    pla = p.for_remote(remote("PLA"))
    assert pla == SetFilament("GFL99", "PLA", "1A1A1AFF",
                              TABLE["GFL99"]["nozzle_min"], TABLE["GFL99"]["nozzle_max"])
    assert p.for_remote(remote("PLA Matte")).tray_info_idx == "GFL99"
    assert p.for_remote(remote("PLA+")).tray_info_idx == "GFL99"
    petg = p.for_remote(remote("PETG"))
    assert petg.tray_info_idx == "GFG99" and petg.tray_type == "PETG"
    assert p.for_remote(remote("PETG-CF")).tray_info_idx == "GFG98"
    assert p.for_remote(remote("TPU")).tray_info_idx == "GFU99"  # 5-char id beats GFSR99
    assert p.for_remote(remote("NYLON-X")) is None
    silk = p.for_remote(remote("PLA", name="Silk Red"))
    assert silk.tray_info_idx == "GFL96"
    assert (silk.nozzle_temp_min, silk.nozzle_temp_max) == (
        TABLE["GFL96"]["nozzle_min"], TABLE["GFL96"]["nozzle_max"])
    assert p.for_remote(remote(color_hex=None)).tray_color == "FFFFFFFF"


def test_override_temps_and_unknown_idx():
    p = Profiles(TABLE, [
        {"filament": "Acme PLA Basic Jet Black", "tray_info_idx": "GFL99",
         "nozzle_temp_min": 200, "nozzle_temp_max": 230},
        {"filament": "Acme NYLON-X Basic Jet Black", "tray_info_idx": "GFN99"},
        {"filament": "Zed Weird", "tray_info_idx": "P4d1f6"},
    ])
    r = p.for_remote(remote())
    assert (r.tray_info_idx, r.nozzle_temp_min, r.nozzle_temp_max) == ("GFL99", 200, 230)
    ny = p.for_remote(remote("NYLON-X", name="NYLON-X Basic Jet Black"))
    assert ny.tray_info_idx == "GFN99" and ny.tray_type == "PA"
    # an override id outside the generic table borrows temps from the material match
    w = p.for_remote(dataclasses.replace(remote("PETG"), brand="Zed", name="Weird"))
    assert w.tray_info_idx == "P4d1f6" and w.nozzle_temp_min == TABLE["GFG99"]["nozzle_min"]
    assert w.tray_type == "PETG"
