"""What DIMORA asks Qwen to do. Each function builds a prompt, calls the model and
validates the JSON it returns (the model never edits the home directly)."""
from __future__ import annotations

from ..config import ROOM_TYPES
from .client import AIClient

TYPES = ", ".join(ROOM_TYPES)


def read_plan(ai: AIClient, image_b64: str, cv, multi: bool = False) -> dict:
    """Name the CV regions, read the scale, find doors/windows/stairs (and floor title)."""
    multi_task = (
        "\n5. This drawing is ONE storey of a multi-storey building. Give \"floor_label\" = the title printed for this plan "
        "(e.g. \"Ground Floor Plan\", \"First Floor\") or null, and \"level\" = 0 for ground/stilt, 1 for first floor, 2 for second, "
        "-1 for basement, or null if unknown.\n6. Give the bbox (percent) of every staircase drawn (the flight with its treads) in \"stairs\". "
        "Name a stair hall \"Staircase\"." if multi else "\n5. Give the bbox (percent) of any staircase drawn, in \"stairs\" ([] if none).")
    extra = ', "stairs":[[x0,y0,x1,y1]]' + (', "floor_label": string|null, "level": number|null' if multi else "")
    if len(cv.rooms) >= 2:
        boxes = "\n".join(f"R{i + 1}: [{round(r['x0'] / cv.W * 100)}, {round(r['y0'] / cv.H * 100)}, {round(r['x1'] / cv.W * 100)}, {round(r['y1'] / cv.H * 100)}]" for i, r in enumerate(cv.rooms))
        prompt = f"""You are an expert architectural floor-plan analyst for an Indian home-design app.
The attached image is a floor plan. Computer vision found these enclosed regions; boxes are [x0, y0, x1, y1] in percent of image width/height:
{boxes}

Tasks:
1. Read the text labels / dimensions printed on the plan and name every region. Choose "type" from: {TYPES}.
2. If dimension annotations are visible, give the real-world width in metres of the built area (left-most to right-most outer wall) as plan_width_m, else null. Convert feet to metres (1 ft = 0.3048 m).
3. If CV clearly missed a room, add it to extra_rooms with a percent bbox.
4. Give approximate centre points (percent of image) of every door and every window you can see.{multi_task}
Respond with ONLY valid JSON, no prose:
{{"plan_width_m": number|null, "rooms":[{{"id":"R1","name":"Living Room","type":"living"}}], "extra_rooms":[{{"name":"","type":"","bbox":[0,0,0,0]}}], "doors":[[x,y]], "windows":[[x,y]]{extra}, "notes":"one sentence"}}"""
    else:
        prompt = f"""You are an expert architectural floor-plan analyst. Detect every room in the attached floor plan.
For each room give name, type (one of: {TYPES}) and bbox [x0, y0, x1, y1] in percent (0-100) of the image width/height, tightly along the wall centre-lines. Rooms should tile the plan without overlapping.
Also give plan_width_m (real width of the built area in metres from dimension labels, else null) and approximate centre points (percent) of doors and windows.{multi_task.replace(chr(10) + '5.', chr(10) + '-').replace(chr(10) + '6.', chr(10) + '-')}
Respond with ONLY valid JSON: {{"plan_width_m": number|null, "rooms_full":[{{"name":"","type":"","bbox":[0,0,0,0]}}], "doors":[[x,y]], "windows":[[x,y]]{extra}, "notes":""}}"""
    out = ai.ask_json(prompt, [image_b64], temperature=0.1, max_tokens=4096)
    return out if isinstance(out, dict) else {}


def split_sheet(ai: AIClient, image_b64: str, n: int):
    prompt = f"""This architectural drawing sheet contains {n} floor plans of one building, one plan per storey (e.g. ground floor, first floor, terrace).
For each separate floor-plan drawing give its bounding box [x0, y0, x1, y1] in percent (0-100) of the image width/height around the walls and dimension lines (not the title block), the title printed for it, and its level (0 = ground/stilt, 1 = first floor, 2 = second, -1 = basement).
Respond with ONLY JSON: {{"floors":[{{"bbox":[0,0,0,0],"label":"Ground Floor Plan","level":0}}]}}"""
    j = ai.ask_json(prompt, [image_b64], temperature=0.1, max_tokens=1200)
    out = []
    for f in (j.get("floors") or []) if isinstance(j, dict) else []:
        b = f.get("bbox")
        if isinstance(b, list) and len(b) == 4 and b[2] > b[0] and b[3] > b[1]:
            out.append({"x0": max(0, b[0] / 100), "y0": max(0, b[1] / 100), "x1": min(1, b[2] / 100), "y1": min(1, b[3] / 100), "label": f.get("label"), "level": f.get("level")})
    return out


def assistant(ai: AIClient, text: str, home_state: str, action_doc: str, history=None) -> dict:
    prompt = f"""You are Aria, the AI home assistant inside DIMORA, an Indian home-design app. You don't just answer — you take actions on the user's 3D home.
Current home state (JSON): {home_state}
{action_doc}
Rules: Map the request to actions. If the user says "this room"/"here", use focus_room. Prices are in Indian Rupees. If the home has several floors, rooms carry a "floor" field and {{"do":"show_floor"}} switches floors. If the user only asks a question, answer it helpfully with actions []. Keep "reply" to 1–3 warm, specific sentences (mention concrete sizes, colours, costs when relevant).
User: {text}
Respond with ONLY JSON: {{"reply":"...","actions":[...]}}"""
    msgs = [{"role": "assistant" if h.get("role") == "model" else "user", "text": h.get("text", "")} for h in (history or [])[-6:]]
    msgs.append({"role": "user", "text": prompt})
    from .client import extract_json
    j = extract_json(ai.chat(msgs, temperature=0.3, max_tokens=1500))
    if not isinstance(j, dict):
        j = {}
    acts = j.get("actions") if isinstance(j.get("actions"), list) else []
    return {"reply": str(j.get("reply") or ""), "actions": [a for a in acts if isinstance(a, dict) and isinstance(a.get("do"), str)]}


def narration(ai: AIClient, theme: str, household: str, floors: list, rooms: list) -> dict:
    facts = "\n".join(f"[{r['id']}] " + (f"({r['floor']}) " if r.get("floor") and len(floors) > 1 else "") + r["facts"] for r in rooms)
    multi = (f"The home has {len(floors)} floors ({', '.join(floors)}) connected by a staircase; the tour climbs the stairs between floors, so mention it when a new floor starts.\n" if len(floors) > 1 else "")
    prompt = f"""You are Aria, a warm, witty Indian interior-design guide giving a spoken, animated home tour in DIMORA.
Home theme: {theme}. Household: {household}.
{multi}Rooms in tour order with facts:
{facts}
Write narration to be read aloud. For each room 2–3 short sentences (max 55 words): describe the space using the real facts, then one practical, specific suggestion. Mention cost in ₹ only for 2–3 rooms. Plain text, no emojis or markdown.
Respond ONLY with JSON: {{"intro":"...","rooms":{{"<id>":"..."}},"outro":"..."}}"""
    j = ai.ask_json(prompt, temperature=0.7, max_tokens=3000)
    ids = {r["id"] for r in rooms}
    return {"intro": str(j.get("intro") or ""), "outro": str(j.get("outro") or ""),
            "rooms": {k: str(v) for k, v in (j.get("rooms") or {}).items() if k in ids and v}}


def inspiration(ai: AIClient, image_b64: str, styles, floors, finishes) -> dict:
    prompt = (f'Analyse this interior inspiration photo. Respond ONLY JSON: {{"style":"one of {", ".join(styles)}","wall":"#hex wall paint colour seen or implied",'
              f'"accent":"#hex","floor":"one of {", ".join(floors)}","furniture_finish":"one of {", ".join(finishes)}","description":"1-2 sentences on what defines this look"}}')
    j = ai.ask_json(prompt, [image_b64], temperature=0.2, max_tokens=800)
    out = {"description": str(j.get("description") or "")}
    if j.get("style") in styles:
        out["style"] = j["style"]
    for k in ("wall", "accent"):
        if isinstance(j.get(k), str) and j[k].startswith("#") and len(j[k]) in (4, 7):
            out[k] = j[k]
    if j.get("floor") in floors:
        out["floor"] = j["floor"]
    if j.get("furniture_finish") in finishes:
        out["finish"] = j["furniture_finish"]
    return out


def report_summary(ai: AIClient, facts: str, room_facts: str) -> str:
    prompt = (f"Write a crisp 4-sentence executive summary for a homeowner's decision report. Facts: {facts} Room facts: {room_facts}. "
              f"Plain text, Indian Rupees, no markdown. Respond ONLY JSON: {{\"summary\":\"...\"}}")
    j = ai.ask_json(prompt, temperature=0.5, max_tokens=600)
    return str(j.get("summary") or "")
