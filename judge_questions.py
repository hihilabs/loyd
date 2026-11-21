"""Every question Loyd's judge can be asked, in one place (TypeSafe's rule: keep
the wording together so it can be compared, versioned and tested).

Bump `version` whenever the wording changes: calibrations and outcomes are kept
per version, so a reworded question starts learning fresh instead of inheriting
a fit that was measured on different words.

Writing rules (from the TypeSafe skill, which suit any model):
  * one judgment per question, no "and"/"or" in the statement;
  * concrete, observable wording; name the confusable case in false_when;
  * phrase so that "yes" means the thing you are looking for.
"""

QUESTIONS = {
    # camagent: the model field of ~24.7k units holds a sticker tag or a label,
    # because the photo was of the wrong thing. Asked of the photo itself.
    "camagent.is_nameplate": {
        "kind": "noul",
        "version": 1,
        "vision": True,
        "statement": "The photo shows an HVAC equipment manufacturer's rating nameplate: the label "
                     "printed by the maker that lists the model number and serial number.",
        "true_when": "such a nameplate is visible and is the main subject of the photo.",
        "false_when": "the photo shows something else: a handwritten or printed location/asset tag "
                      "(like 'AHU-1' or 'RTU 3'), a thermostat, an electrical part or wiring, a whole "
                      "unit or roof with no readable nameplate, or a label too blurry to read.",
    },
    # camagent: a tonnage read off a plate is only evidence if the plate states it.
    "camagent.plate_states_capacity": {
        "kind": "noul",
        "version": 1,
        "vision": True,
        "statement": "This nameplate prints the unit's cooling capacity.",
        "true_when": "the plate shows a capacity in TONS, or a cooling capacity in BTU/h or BTUH.",
        "false_when": "the plate shows only electrical ratings (volts, amps, RLA, FLA, LRA, MCA, MOCP), "
                      "refrigerant charge, or pressures, and no cooling capacity.",
    },
    # Loyd itself: brain.classify's routing question, as a probability instead of text.
    "loyd.routine_question": {
        "kind": "noul",
        "version": 1,
        "vision": False,
        "statement": "This question to an ops assistant can be answered with a quick routine check "
                     "of the systems.",
        "true_when": "it asks for a status, a value, a log line or a simple fact about the stack.",
        "false_when": "it needs research, design, debugging across several systems, or writing code.",
    },
}
