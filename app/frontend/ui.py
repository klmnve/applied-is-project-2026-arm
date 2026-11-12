"""Run from project root: python -m streamlit run app/frontend/ui.py."""

import os
from typing import Any, cast

import streamlit as st

from app.frontend.client import APIClient, APIError

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def reset_login(message: str, kind: str = "warning") -> None:
    client = st.session_state.get("api")
    if isinstance(client, APIClient):
        client.close()
    st.session_state.clear()
    st.session_state["flash"] = (kind, message)


def show_error(error: APIError) -> None:
    if error.status == 401:
        reset_login("Сессия аяқталды немесе тоқтатылды. Жүйеге қайта кіріңіз.")
        st.rerun()
    st.error(str(error))


def saved(message: str) -> None:
    st.session_state["reset_editor"] = True
    st.session_state["flash"] = ("success", message)
    st.rerun()


def first_page() -> None:
    st.session_state["resource_page"] = 1


def clear_editor() -> None:
    for key in list(st.session_state):
        if str(key).startswith("edit_") and key != "edit_resource":
            del st.session_state[key]


def clear_delete_confirmation() -> None:
    for key in list(st.session_state):
        if str(key).startswith("delete_confirm_"):
            del st.session_state[key]


def refresh_catalog() -> None:
    clear_editor()
    clear_delete_confirmation()


def resource_fields(
    prefix: str,
    categories: dict[int, str],
    rooms: dict[int, str],
    item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = item or {}
    left, right = st.columns(2)
    with left:
        name = st.text_input(
            "Жабдық атауы", value=row.get("name", ""), max_chars=160, key=f"{prefix}_name"
        )
        category_ids = list(categories)
        category = st.selectbox(
            "Санат",
            category_ids,
            index=(category_ids.index(row["category_id"]) if item else None),
            format_func=lambda key: categories[key],
            placeholder="Санатты таңдаңыз",
            key=f"{prefix}_category",
        )
        quantity = st.number_input(
            "Саны",
            min_value=0,
            max_value=1_000_000_000,
            value=int(row.get("quantity", 1)),
            step=1,
            key=f"{prefix}_quantity",
        )
    with right:
        inventory = st.text_input(
            "Түгендеу коды",
            value=row.get("inventory_code", ""),
            max_chars=64,
            key=f"{prefix}_inventory",
        )
        room_ids = list(rooms)
        room = st.selectbox(
            "Бөлме",
            room_ids,
            index=(room_ids.index(row["room_id"]) if item else None),
            format_func=lambda key: rooms[key],
            placeholder="Бөлмені таңдаңыз",
            key=f"{prefix}_room",
        )
        cost = st.number_input(
            "Бірлік құны, ₸",
            min_value=0.0,
            max_value=1_000_000_000_000.0,
            value=float(row.get("unit_cost", 0)),
            step=100.0,
            key=f"{prefix}_cost",
        )
    return {
        "name": name.strip(),
        "inventory_code": inventory.strip(),
        "category_id": category,
        "room_id": room,
        "quantity": quantity,
        "unit_cost": cost,
    }


def valid_form(payload: dict[str, Any]) -> bool:
    if not payload["name"] or not payload["inventory_code"]:
        st.error("Жабдық атауы мен түгендеу кодын толтырыңыз.")
        return False
    if payload["category_id"] is None or payload["room_id"] is None:
        st.error("Санат пен бөлмені таңдаңыз.")
        return False
    return True


st.set_page_config(
    page_title="Кафедра жабдықтары · АРМ", page_icon=":material/inventory_2:", layout="wide"
)
if "api" not in st.session_state:
    st.session_state["api"] = APIClient(BACKEND_URL)
st.session_state.setdefault("user", None)
st.session_state.setdefault("resource_page", 1)
if st.session_state.pop("reset_editor", False):
    refresh_catalog()
api = cast(APIClient, st.session_state["api"])

with st.sidebar:
    st.title("Кафедра АРМ")
    st.caption("Жабдықтарды есепке алу")
    st.link_button(
        "Негізгі АРМ", PUBLIC_BACKEND_URL, icon=":material/open_in_new:", width="stretch"
    )
    st.link_button(
        "API құжаттамасы", f"{PUBLIC_BACKEND_URL}/docs", icon=":material/code:", width="stretch"
    )

st.title("Кафедра жабдықтары")
st.caption("Жабдықтар, бөлмелер және санаттар бір тізімде.")
flash = st.session_state.pop("flash", None)
if flash:
    getattr(st, flash[0])(flash[1])

if st.session_state["user"] is None:
    st.subheader("Жүйеге кіру")
    st.write("Кафедра АРМ-ындағы өз аккаунтыңызбен кіріңіз.")
    with st.form("login_form", clear_on_submit=True):
        login = st.text_input("Логин", key="login_name", max_chars=64)
        password = st.text_input("Құпиясөз", type="password", key="login_password", max_chars=128)
        submitted = st.form_submit_button("Кіру", type="primary", key="login_submit")
    if submitted:
        if not login.strip() or not password:
            st.error("Логин мен құпиясөзді енгізіңіз.")
        else:
            try:
                result = api.request(
                    "POST", "/api/auth/login", json={"login": login.strip(), "password": password}
                )
                st.session_state["user"] = result["user"]
                st.rerun()
            except APIError as error:
                st.error(str(error))
    st.caption(
        "Жаңа аккаунтты негізгі АРМ-да тіркеуге болады. "
        "Жабдықтарды қарау құқығын кафедра әкімшісі береді."
    )
    st.stop()

with st.sidebar:
    if st.button("Шығу", key="logout", icon=":material/logout:", width="stretch"):
        try:
            api.request("POST", "/api/auth/logout")
            reset_login("Жүйеден шықтыңыз.", "success")
        except APIError as error:
            if error.status == 401:
                reset_login("Жүйеден шықтыңыз.", "success")
            else:
                reset_login(
                    "Осы интерфейстен шықтыңыз. Серверге қосылмағандықтан, "
                    "сервердегі сессияның тоқтатылғанын растау мүмкін болмады."
                )
        st.rerun()

try:
    # Re-read permissions on every interaction, including after administrator changes.
    user = api.request("GET", "/api/auth/me")["user"]
    st.session_state["user"] = user
except APIError as error:
    show_error(error)
    if st.button("Қайта қосылу", key="retry_connection"):
        st.rerun()
    st.stop()

st.sidebar.write(user["display_name"])
permissions = set(user.get("permissions", []))
if "Documents.View" not in permissions:
    status = user.get("approval_status", "approved")
    if status == "pending":
        st.info("Өтініміңіз әкімшінің мақұлдауын күтуде. Күйін негізгі АРМ-нан көре аласыз.")
    elif status == "rejected":
        st.warning("Өтінім қабылданбаған. Толық мәліметті негізгі АРМ-нан қараңыз.")
    else:
        st.info(
            "Сіздің рөліңізге жабдықтар тізімін қарау құқығы берілмеген. "
            "Кафедра әкімшісіне хабарласыңыз."
        )
    st.stop()
can_manage = user.get("role_code") == "admin" and "Users.Manage" in permissions

try:
    categories = {
        row["id"]: row["name"] for row in api.request("GET", "/api/resource-categories")["items"]
    }
    rooms = {row["id"]: row["name"] for row in api.request("GET", "/api/resource-rooms")["items"]}
except APIError as error:
    show_error(error)
    st.stop()

with st.container(horizontal=True, vertical_alignment="bottom"):
    search = st.text_input(
        "Атауы немесе түгендеу коды",
        key="search",
        max_chars=160,
        on_change=first_page,
        placeholder="Мысалы, проектор",
    )
    category_filter = st.selectbox(
        "Санат бойынша",
        [None, *categories],
        format_func=lambda key: categories.get(key, "Барлық санаттар"),
        key="category_filter",
        on_change=first_page,
    )
    room_filter = st.selectbox(
        "Бөлме бойынша",
        [None, *rooms],
        format_func=lambda key: rooms.get(key, "Барлық бөлмелер"),
        key="room_filter",
        on_change=first_page,
    )
    st.button("Жаңарту", key="refresh", icon=":material/refresh:", on_click=refresh_catalog)

cost_filter = st.toggle("Құн аралығы бойынша сүзу", key="cost_filter", on_change=first_page)
params: dict[str, Any] = {"search": search, "page_size": 25}
if category_filter is not None:
    params["category_id"] = category_filter
if room_filter is not None:
    params["room_id"] = room_filter
if cost_filter:
    with st.container(horizontal=True):
        minimum = st.number_input(
            "Ең төменгі құн, ₸", min_value=0.0, value=0.0, key="min_cost", on_change=first_page
        )
        maximum = st.number_input(
            "Ең жоғарғы құн, ₸",
            min_value=0.0,
            value=1_000_000.0,
            key="max_cost",
            on_change=first_page,
        )
    if minimum > maximum:
        st.error("Ең төменгі құн ең жоғарғы құннан аспауы керек.")
        st.stop()
    params.update(min_cost=minimum, max_cost=maximum)
page = st.number_input("Бет", min_value=1, max_value=1_000_000, step=1, key="resource_page")
params["page"] = page
try:
    listing = api.request("GET", "/api/resources", params=params)
except APIError as error:
    show_error(error)
    st.stop()
items = listing["items"]
st.caption(f"Табылды: {listing['total']} · Бет: {page} / {max(1, listing['pages'])}")
if items:
    table = [
        {
            "ID": row["id"],
            "Атауы": row["name"],
            "Түгендеу коды": row["inventory_code"],
            "Санат": row["category_name"],
            "Бөлме": row["room_name"],
            "Саны": row["quantity"],
            "Бірлік құны, ₸": row["unit_cost"],
        }
        for row in items
    ]
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        alt="Кафедра жабдықтарының тізімі",
        column_config={"Бірлік құны, ₸": st.column_config.NumberColumn(format="%.2f")},
    )
else:
    st.info("Жабдық табылмады. Сүзгілерді немесе бет нөмірін өзгертіңіз.")

if not can_manage:
    st.caption("Қарау режимі. Жабдықтарды кафедра әкімшісі басқарады.")
    st.stop()
if not categories or not rooms:
    st.warning("Санаттар немесе бөлмелер анықтамалығы бос. Әкімшіге хабарласыңыз.")
    st.stop()

st.subheader("Жабдықтарды басқару")
add_tab, edit_tab, delete_tab = st.tabs(
    ["Қосу", "Өзгерту", "Жою"], key="management_tab", on_change="rerun"
)
with add_tab:
    with st.form("add_resource", clear_on_submit=True):
        payload = resource_fields("add", categories, rooms)
        add_submitted = st.form_submit_button("Жабдық қосу", type="primary", key="add_submit")
    if add_submitted and valid_form(payload):
        try:
            api.request("POST", "/api/resources", json=payload)
            saved("Жабдық қосылды. Тізім жаңартылды.")
        except APIError as error:
            show_error(error)

item_map = {row["id"]: row for row in items}
with edit_tab:
    if not items:
        st.info("Өзгерту үшін тізімнен жабдық табыңыз.")
    else:
        selected = st.selectbox(
            "Өзгертілетін жабдық",
            list(item_map),
            key="edit_resource",
            on_change=clear_editor,
            format_func=lambda key: f"{item_map[key]['inventory_code']} · {item_map[key]['name']}",
        )
        assert selected is not None
        snapshot = st.session_state.get("edit_snapshot")
        if snapshot is None or snapshot["id"] != selected:
            snapshot = dict(item_map[selected])
            st.session_state["edit_snapshot"] = snapshot
        with st.form(f"edit_resource_form_{selected}"):
            edited = resource_fields(f"edit_{selected}", categories, rooms, snapshot)
            edit_submitted = st.form_submit_button(
                "Өзгерісті сақтау", type="primary", key="edit_submit"
            )
        if edit_submitted and valid_form(edited):
            changes = {key: value for key, value in edited.items() if value != snapshot[key]}
            if not changes:
                st.info("Өзгертілген өріс жоқ.")
            else:
                try:
                    api.request("PATCH", f"/api/resources/{selected}", json=changes)
                    saved("Өзгерістер сақталды. Тізім жаңартылды.")
                except APIError as error:
                    show_error(error)
with delete_tab:
    if not items:
        st.info("Жою үшін тізімнен жабдық табыңыз.")
    else:
        deleting = st.selectbox(
            "Жойылатын жабдық",
            list(item_map),
            key="delete_resource",
            on_change=clear_delete_confirmation,
            format_func=lambda key: f"{item_map[key]['inventory_code']} · {item_map[key]['name']}",
        )
        assert deleting is not None
        st.warning("Жазба дерекқордан біржола жойылады. Әрекет аудит журналында сақталады.")
        confirmed = st.checkbox("Осы жабдықты жоюды растаймын", key=f"delete_confirm_{deleting}")
        if st.button("Жабдықты жою", key=f"delete_button_{deleting}", disabled=not confirmed):
            try:
                api.request("DELETE", f"/api/resources/{deleting}")
                saved("Жабдық жойылды. Тізім жаңартылды.")
            except APIError as error:
                show_error(error)
