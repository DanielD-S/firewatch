from firewatch import conditions as c


def test_ignition_probability_monotonic():
    cold_wet = c.ignition_probability(10.0, 80.0, is_day=True)
    warm = c.ignition_probability(25.0, 40.0, is_day=True)
    hot_dry = c.ignition_probability(36.0, 12.0, is_day=True)
    assert cold_wet < warm < hot_dry
    assert hot_dry >= 70.0
    assert cold_wet < 25.0
    # referencias NFDRS: FM 20 % -> ~5 %, FM 10 % a 80 °F -> ~40 %
    assert c.ignition_probability(5.0, 95.0, is_day=False) < 10.0


def test_night_lower_than_day():
    assert c.ignition_probability(30.0, 20.0, is_day=False) < c.ignition_probability(30.0, 20.0, is_day=True)


def test_rule_303030_and_boton_rojo():
    h = c.hour_condition("2026-01-15T15:00", 15, 34.0, 18.0, 32.0, 45.0, 200.0, 0.0, True)
    assert h.rule_303030
    assert h.rule_boton_rojo
    assert h.score >= 0.7
    # misma condición a las 10:00: 30-30-30 sí, Botón Rojo no (ventana 14-18)
    h2 = c.hour_condition("2026-01-15T10:00", 10, 34.0, 18.0, 32.0, 45.0, 200.0, 0.0, True)
    assert h2.rule_303030 and not h2.rule_boton_rojo
    # noche húmeda: nada
    h3 = c.hour_condition("2026-01-15T03:00", 3, 12.0, 85.0, 5.0, 8.0, 200.0, 0.0, False)
    assert not h3.rule_303030 and not h3.rule_boton_rojo
    assert h3.score < 0.3


def test_rain_dampens_score():
    dry = c.hour_condition("t", 15, 30.0, 25.0, 25.0, 30.0, 0.0, 0.0, True)
    wet = c.hour_condition("t", 15, 30.0, 25.0, 25.0, 30.0, 0.0, 3.0, True)
    assert wet.score < dry.score
