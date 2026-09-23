# -*- coding: utf-8 -*-
"""
Integración Control de Ruta <-> SimpliRoute (SR)
=================================================

Diseño acordado (ver conversación de referencia):

- VEHÍCULOS: híbrido. Control de Ruta es la fuente de creación. Al guardar un
  camión aquí (con transportista, piloto, auxiliar ya resueltos), se manda a
  SR. SR nunca crea camiones por su cuenta — solo recibe.

- PILOTOS: fuente es SR. Carga inicial manual (vincular id_sr a mano una vez).
  De ahí en adelante, sincronización de SOLO LECTURA desde SR (GET drivers):
  actualiza los ya vinculados y crea como "pendiente de completar" los que
  aparezcan nuevos en SR. Control de Ruta NUNCA crea drivers en SR.

- RUTAS: al crear la ruta en SR se manda `reference` = folio del viaje de
  Control de Ruta (id_viaje), para que el mismo identificador se pueda buscar
  en ambos sistemas. El UUID técnico que devuelve SR se guarda en
  `viajes.route_id_sr` para las consultas posteriores (ej. traer el km).

- KM: el campo `kilometers` del objeto Ruta en SR viene en null al crear la
  ruta. Se debe re-consultar más adelante (GET) cuando el viaje se marque
  como Finalizado. Todavía no confirmado si depende del módulo de GPS de SR
  — por eso esta función se aísla, para poder cambiarla sin tocar el resto.

Reglas no negociables (mismo patrón que ya usa `enviar_visita_simpliroute`
en app.py):
  - Ninguna llamada a SR puede tumbar una operación de Control de Ruta que
    ya se guardó localmente. Todo esto es "best effort": si SR falla, se
    registra y la app sigue funcionando 100% manual.
  - Timeout corto (8s) en cada llamada — nunca el default de `requests`.
  - Nunca se lanza una excepción hacia quien llama: todo devuelve
    (ok: bool, resultado_o_mensaje).
"""

import requests
from datetime import datetime

# ---------------------------------------------------------------------------
# Config / cliente HTTP
# ---------------------------------------------------------------------------

BASE_URL = "https://api.simpliroute.com/v1/"
TIMEOUT_SEGUNDOS = 8

# Patrón de placas "dummy" de planeación en SR: C- seguido de 6 dígitos
# (ej. C-123456). Las placas reales de Ransa son C- + 3 dígitos + 3 letras
# (ej. C-896BYM), así que no hay colisión posible entre ambos formatos.
import re
PATRON_PLACA_DUMMY_SR = re.compile(r'^C-\d{6}$')


def _obtener_token():
    """Aísla de dónde sale el token — hoy st.secrets, pero así se puede
    cambiar sin tocar el resto del módulo. Se importa streamlit adentro de
    la función (no arriba del archivo) para que este módulo se pueda probar
    con pytest/unittest sin necesitar un proceso de Streamlit corriendo."""
    import streamlit as st
    return st.secrets["simpliroute"]["api_token"]


def _headers(token=None):
    token = token or _obtener_token()
    return {"Authorization": f"Token {token}", "Content-Type": "application/json"}


def _sr_request(method, ruta, token=None, **kwargs):
    """Wrapper único para todas las llamadas a la API de SR.

    Devuelve siempre (ok: bool, data_o_mensaje). Nunca lanza una excepción:
    - Error de red / timeout -> (False, mensaje corto y claro)
    - HTTP 4xx/5xx            -> (False, mensaje con el código y el body de SR)
    - HTTP 2xx                -> (True, json ya parseado)
    """
    url = BASE_URL + ruta.lstrip("/")
    try:
        resp = requests.request(
            method, url, headers=_headers(token), timeout=TIMEOUT_SEGUNDOS, **kwargs
        )
    except requests.exceptions.Timeout:
        return False, "SimpliRoute no respondió a tiempo (timeout de 8s)."
    except requests.exceptions.RequestException as e:
        return False, f"No se pudo conectar con SimpliRoute: {e}"

    if resp.ok:
        try:
            return True, resp.json()
        except ValueError:
            return True, None  # 204 No Content, por ejemplo (delete)
    else:
        try:
            detalle = resp.json()
        except ValueError:
            detalle = resp.text
        return False, f"SimpliRoute respondió HTTP {resp.status_code}: {detalle}"


def es_placa_dummy_sr(license_plate):
    """True si el valor de license_plate corresponde a un vehículo 'dummy' de
    planeación en SR (sin placa real confirmada todavía)."""
    if not license_plate or not str(license_plate).strip():
        return True
    return bool(PATRON_PLACA_DUMMY_SR.match(str(license_plate).strip().upper()))


def normalizar_placa(placa):
    """Mayúsculas, sin espacios NI guiones sueltos, y reinserta el guión en
    su posición fija (C-XXXYYY) — para que 'c 896bym', 'C896BYM',
    'c-896bym' y 'C-896BYM' siempre resuelvan al mismo valor, tanto al
    guardar en Control de Ruta como al comparar/mandar a SR."""
    if not placa:
        return placa
    limpio = str(placa).strip().upper().replace(" ", "").replace("-", "")
    if limpio.startswith("C") and len(limpio) == 7:  # C + 3 dígitos + 3 letras (o 6 dígitos si es dummy)
        return "C-" + limpio[1:]
    return limpio


def buscar_vehiculo_sr_por_placa(placa, token=None):
    """GET /routes/vehicles/ y busca coincidencia exacta por license_plate
    (ya normalizada). Se usa SOLO la primera vez que un camión se sincroniza
    (cuando todavía no tenemos su id_sr) — para no crear un duplicado si
    alguien ya lo había dado de alta manualmente en SR antes de hoy.
    Nunca se debe volver a llamar una vez que el camión ya tiene id_sr —
    ahí el camino correcto es actualizar_vehiculo_sr() directo por id_sr."""
    placa_norm = normalizar_placa(placa)
    ok, data = _sr_request("GET", "routes/vehicles/", token=token)
    if not ok:
        return False, data
    lista = data.get("results", data) if isinstance(data, dict) else data
    for v in lista:
        if normalizar_placa(v.get("license_plate")) == placa_norm:
            return True, v.get("id")
    return True, None  # búsqueda exitosa, simplemente no existe todavía


# ---------------------------------------------------------------------------
# VEHÍCULOS — Control de Ruta manda, SR solo recibe
# ---------------------------------------------------------------------------

def _payload_vehiculo(placa, tipo, id_sr_piloto):
    """Payload común para crear/actualizar un vehículo en SR a partir de los
    datos que ya viven en cat_camiones. `id_sr_piloto` puede ser None si el
    piloto asignado todavía no tiene vínculo con SR — en ese caso se manda
    default_driver=None (SR permite el vehículo sin conductor por defecto)."""
    placa = normalizar_placa(placa)
    return {
        "name": placa,
        "license_plate": placa,
        "reference_id": placa,
        "default_driver": id_sr_piloto,
    }


def crear_vehiculo_sr(placa, tipo, id_sr_piloto=None, token=None):
    """POST /routes/vehicles/ — usar SOLO cuando el camión todavía no tiene
    id_sr guardado en cat_camiones (primera vez que se sincroniza)."""
    payload = _payload_vehiculo(placa, tipo, id_sr_piloto)
    ok, data = _sr_request("POST", "routes/vehicles/", token=token, json=payload)
    if not ok:
        return False, data
    # La API a veces envuelve la respuesta en una lista de un solo elemento.
    vehiculo = data[0] if isinstance(data, list) else data
    id_sr = vehiculo.get("id")
    if id_sr is None:
        return False, f"SimpliRoute no devolvió un id de vehículo válido: {data}"
    return True, id_sr


def actualizar_vehiculo_sr(id_sr, placa, tipo, id_sr_piloto=None, token=None):
    """PATCH /routes/vehicles/{id_sr}/ — usar cuando el camión YA tiene id_sr
    (por ejemplo, cambió de piloto asignado)."""
    payload = _payload_vehiculo(placa, tipo, id_sr_piloto)
    ok, data = _sr_request("PATCH", f"routes/vehicles/{id_sr}/", token=token, json=payload)
    if not ok:
        return False, data
    return True, id_sr


def sincronizar_camion_con_sr(get_conn_fn, placa, token=None):
    """Función de alto nivel: lee el camión y su piloto desde la base local,
    decide si hay que CREAR o ACTUALIZAR en SR, y si sale bien, guarda el
    id_sr/estado en cat_camiones. `get_conn_fn` se recibe como parámetro (en
    vez de importar get_conn directo de app.py) para que este módulo se
    pueda probar sin depender de una base de datos real.

    Devuelve (ok: bool, mensaje: str). Nunca lanza — si algo falla, el
    camión se queda guardado localmente con sincronizado_sr=False, y el
    resto de Control de Ruta sigue funcionando normal."""
    from contextlib import closing

    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT placa, tipo, piloto, id_sr FROM cat_camiones WHERE placa = %s",
            (placa,)
        )
        fila = cur.fetchone()
        if not fila:
            return False, f"El camión con placa {placa} no existe en cat_camiones."
        placa_db, tipo, piloto, id_sr_actual = fila

        id_sr_piloto = None
        if piloto:
            cur.execute("SELECT id_sr FROM cat_pilotos WHERE nombre = %s", (piloto,))
            fila_piloto = cur.fetchone()
            if fila_piloto:
                id_sr_piloto = fila_piloto[0]

        if id_sr_actual:
            # Ya tenemos su id_sr de una sincronización anterior — NUNCA se
            # vuelve a buscar por placa aquí (eso arriesgaría crear un
            # duplicado si la placa cambió). Se actualiza directo por id_sr.
            ok, resultado = actualizar_vehiculo_sr(id_sr_actual, placa_db, tipo, id_sr_piloto, token=token)
            id_sr_nuevo = id_sr_actual if ok else id_sr_actual
        else:
            # Primera vez que este camión se sincroniza: buscamos por placa
            # por si alguien ya lo dio de alta manualmente en SR antes de
            # hoy — para no crear un duplicado.
            ok_busqueda, id_existente = buscar_vehiculo_sr_por_placa(placa_db, token=token)
            if not ok_busqueda:
                ok, resultado, id_sr_nuevo = False, f"No se pudo verificar si ya existía en SR: {id_existente}", None
            elif id_existente:
                ok, resultado = actualizar_vehiculo_sr(id_existente, placa_db, tipo, id_sr_piloto, token=token)
                id_sr_nuevo = id_existente if ok else None
            else:
                ok, resultado = crear_vehiculo_sr(placa_db, tipo, id_sr_piloto, token=token)
                id_sr_nuevo = resultado if ok else None

        try:
            if ok:
                cur.execute(
                    "UPDATE cat_camiones SET id_sr = %s, sincronizado_sr = TRUE, "
                    "fecha_sincronizacion_sr = %s WHERE placa = %s",
                    (id_sr_nuevo, datetime.now(), placa_db)
                )
            else:
                cur.execute(
                    "UPDATE cat_camiones SET sincronizado_sr = FALSE WHERE placa = %s",
                    (placa_db,)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, f"Se sincronizó con SR pero falló al guardar localmente: {e}"

        if ok:
            return True, f"Vehículo sincronizado con SR (id_sr={id_sr_nuevo})."
        return False, f"No se pudo sincronizar con SR: {resultado}"


def reintentar_camiones_pendientes(get_conn_fn, token=None):
    """Recorre cat_camiones buscando los que están activos y NO sincronizados
    (sincronizado_sr = FALSE o id_sr IS NULL) y reintenta uno por uno. Pensada
    para el botón 'Reintentar sincronización' del panel de Administrador."""
    from contextlib import closing

    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT placa FROM cat_camiones WHERE activo = TRUE "
            "AND (sincronizado_sr IS NOT TRUE OR id_sr IS NULL)"
        )
        placas_pendientes = [r[0] for r in cur.fetchall()]

    resultados = {}
    for placa in placas_pendientes:
        ok, msg = sincronizar_camion_con_sr(get_conn_fn, placa, token=token)
        resultados[placa] = (ok, msg)
    exitosos = sum(1 for ok, _ in resultados.values() if ok)
    return resultados, f"{exitosos} de {len(resultados)} camión(es) pendiente(s) sincronizados."


# ---------------------------------------------------------------------------
# PILOTOS — SR manda, Control de Ruta solo recibe (sincronización de lectura)
# ---------------------------------------------------------------------------

def obtener_drivers_sr(token=None):
    """GET /accounts/drivers/ — lista cruda de conductores en SR."""
    ok, data = _sr_request("GET", "accounts/drivers/", token=token)
    if not ok:
        return False, data
    lista = data.get("results", data) if isinstance(data, dict) else data
    # Solo los que de verdad son conductores de reparto (is_driver=True) —
    # una cuenta de SR puede tener usuarios admin/router mezclados en el
    # mismo listado.
    return True, [d for d in lista if d.get("is_driver", True)]


def sincronizar_pilotos_desde_sr(get_conn_fn, token=None):
    """Trae los drivers de SR y los refleja en cat_pilotos:
      - Si el id_sr ya está vinculado a un piloto -> actualiza el NOMBRE
        (por si lo corrigieron en SR) sin tocar 'activo'.
      - Si el id_sr es nuevo Y el nombre no choca con ningún piloto local
        ya existente -> crea el piloto marcado pendiente_completar=TRUE y
        activo=FALSE, para que un Admin lo complete antes de asignarlo.
      - Si el id_sr es nuevo PERO el nombre coincide con un piloto local que
        todavía no tiene id_sr -> NO se fusiona solo (podrían ser dos
        personas distintas con el mismo nombre). Se manda a una cola de
        revisión (`cat_pilotos_revision_nombre`) para que un Operador/
        Supervisor decida: ¿es la misma persona (vincular) o es otra
        (crear aparte)?
    Nunca borra ni desactiva pilotos por su cuenta — eso queda para revisión
    manual, porque el objeto driver de SR no trae un estado activo/inactivo
    confiable para decidir eso automáticamente."""
    from contextlib import closing

    ok, drivers = obtener_drivers_sr(token=token)
    if not ok:
        return False, f"No se pudo consultar SimpliRoute: {drivers}"

    nuevos, actualizados, en_revision, con_error = 0, 0, 0, []
    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        for d in drivers:
            id_sr = d.get("id")
            nombre_sr = (d.get("name") or "").strip() or d.get("username", f"Piloto SR {id_sr}")
            try:
                cur.execute("SELECT nombre FROM cat_pilotos WHERE id_sr = %s", (id_sr,))
                fila = cur.fetchone()
                if fila:
                    cur.execute(
                        "UPDATE cat_pilotos SET nombre = %s, fecha_sincronizacion_sr = %s WHERE id_sr = %s",
                        (nombre_sr, datetime.now(), id_sr)
                    )
                    actualizados += 1
                    continue

                # id_sr nuevo — ¿el nombre choca con un piloto local sin vincular?
                cur.execute(
                    "SELECT nombre FROM cat_pilotos WHERE nombre = %s AND id_sr IS NULL",
                    (nombre_sr,)
                )
                choque = cur.fetchone()
                if choque:
                    cur.execute(
                        "INSERT INTO cat_pilotos_revision_nombre (nombre, id_sr, fecha_deteccion) "
                        "VALUES (%s, %s, %s) ON CONFLICT (id_sr) DO NOTHING",
                        (nombre_sr, id_sr, datetime.now())
                    )
                    en_revision += 1
                else:
                    cur.execute(
                        "INSERT INTO cat_pilotos (nombre, id_sr, activo, pendiente_completar, "
                        "fecha_sincronizacion_sr) VALUES (%s, %s, FALSE, TRUE, %s) "
                        "ON CONFLICT (nombre) DO NOTHING",
                        (nombre_sr, id_sr, datetime.now())
                    )
                    nuevos += 1
            except Exception as e:
                con_error.append((id_sr, str(e)))
        conn.commit()

        cur.execute(
            "INSERT INTO control_sincronizacion_sr (proceso, ultima_ejecucion, resultado) "
            "VALUES ('pilotos', %s, %s) "
            "ON CONFLICT (proceso) DO UPDATE SET ultima_ejecucion = EXCLUDED.ultima_ejecucion, "
            "resultado = EXCLUDED.resultado",
            (datetime.now(), f"{nuevos} nuevo(s), {actualizados} actualizado(s), "
                              f"{en_revision} en revisión, {len(con_error)} con error")
        )
        conn.commit()

    resumen = (f"{nuevos} piloto(s) nuevo(s) (pendientes de completar), "
               f"{actualizados} actualizado(s), {en_revision} en revisión por nombre duplicado.")
    if con_error:
        resumen += f" {len(con_error)} con error — revisar logs."
    return True, resumen


def debe_sincronizar_pilotos(get_conn_fn, horas=12):
    """True si nunca se ha sincronizado, o si pasaron >= `horas` desde la
    última vez — para el disparo automático al abrir la app."""
    from contextlib import closing

    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT ultima_ejecucion FROM control_sincronizacion_sr WHERE proceso = 'pilotos'")
        fila = cur.fetchone()
    if not fila or not fila[0]:
        return True
    return (datetime.now() - fila[0]).total_seconds() >= horas * 3600


# ---------------------------------------------------------------------------
# RUTAS — folio de Control de Ruta como `reference`, y km de vuelta
# ---------------------------------------------------------------------------

def crear_ruta_sr(id_viaje, id_sr_vehiculo, id_sr_piloto, planned_date,
                   origen_direccion, origen_lat, origen_lon,
                   destino_direccion, destino_lat, destino_lon,
                   plan_id, hora_inicio="07:00:00", hora_fin="17:00:00", token=None):
    """POST /routes/routes/ — crea la ruta en SR con `reference` = folio del
    viaje de Control de Ruta, para que el mismo identificador se pueda
    buscar en ambos sistemas. Devuelve (True, route_id_uuid) o (False, msg)."""
    payload = {
        "plan": plan_id,
        "vehicle": id_sr_vehiculo,
        "driver": id_sr_piloto,
        "planned_date": planned_date,
        "estimated_time_start": hora_inicio,
        "estimated_time_end": hora_fin,
        "location_start_address": origen_direccion,
        "location_start_latitude": origen_lat,
        "location_start_longitude": origen_lon,
        "location_end_address": destino_direccion,
        "location_end_latitude": destino_lat,
        "location_end_longitude": destino_lon,
        "reference": id_viaje,
    }
    ok, data = _sr_request("POST", "routes/routes/", token=token, json=payload)
    if not ok:
        return False, data
    ruta = data[0] if isinstance(data, list) else data
    route_id = ruta.get("id")
    if not route_id:
        return False, f"SimpliRoute no devolvió un id de ruta válido: {data}"
    return True, route_id


def importar_rutas_sr(fecha, token=None):
    """Trae todas las Rutas de SR de una fecha, y por cada una agrupa sus
    Visitas por tienda (título), sumando `load_3` como cajas — el mismo
    proceso que ya validamos a mano con datos reales. NO toca la base de
    datos ni decide nada de negocio — solo arma la estructura cruda para
    que app.py la muestre en la cola de 'Rutas pendientes de importar'.

    Devuelve (True, [ruta_dict, ...]) o (False, mensaje). Cada ruta_dict:
      {
        "route_id": uuid de la Ruta en SR,
        "vehicle_sr_id": id del vehículo en SR (puede ser el dummy),
        "driver_sr_id": id del piloto en SR,
        "total_visitas": int,
        "visit_types": {tipo: conteo},  # para más adelante cruzar con cat_mapeo_cliente_sr
        "destinos": [
            {"tienda": str, "cajas": int, "pedidos": [{"pedido": reference, "cajas": n, "visit_id": id}]}
        ],
      }
    """
    ok, rutas_raw = _sr_request("GET", "routes/routes/", token=token, params={"planned_date": fecha})
    if not ok:
        return False, f"No se pudieron traer las Rutas de SimpliRoute: {rutas_raw}"
    lista_rutas = rutas_raw.get("results", rutas_raw) if isinstance(rutas_raw, dict) else rutas_raw

    ok, visitas_raw = _sr_request("GET", "routes/visits/", token=token, params={"planned_date": fecha})
    if not ok:
        return False, f"No se pudieron traer las Visitas de SimpliRoute: {visitas_raw}"
    lista_visitas = visitas_raw.get("results", visitas_raw) if isinstance(visitas_raw, dict) else visitas_raw

    resultado = []
    for ruta in lista_rutas:
        route_id = ruta.get("id")
        visitas_de_ruta = [v for v in lista_visitas if v.get("route") == route_id]
        if not visitas_de_ruta:
            continue  # ruta sin visitas ese día — nada que importar

        visit_types = {}
        por_tienda = {}
        for v in visitas_de_ruta:
            tipo = v.get("visit_type") or "(sin tipo)"
            visit_types[tipo] = visit_types.get(tipo, 0) + 1

            tienda = v.get("title") or "(sin nombre)"
            if tienda not in por_tienda:
                por_tienda[tienda] = {"tienda": tienda, "cajas": 0, "pedidos": []}
            cajas_visita = v.get("load_3") or 0
            por_tienda[tienda]["cajas"] += cajas_visita
            por_tienda[tienda]["pedidos"].append({
                "pedido": v.get("reference"),
                "cajas": cajas_visita,
                "visit_id": v.get("id"),
                "origen": "SR",
            })

        resultado.append({
            "route_id": route_id,
            "vehicle_sr_id": ruta.get("vehicle"),
            "driver_sr_id": ruta.get("driver"),
            "total_visitas": len(visitas_de_ruta),
            "visit_types": visit_types,
            "destinos": list(por_tienda.values()),
        })

    return True, resultado


def reasignar_vehiculo_piloto_ruta_sr(route_id, id_sr_vehiculo, id_sr_piloto, token=None):
    """PATCH a una Ruta existente para reemplazar el vehículo/piloto (ej. el
    dummy de Infor) por el camión/piloto real que se asignó en Control de
    Ruta. Nunca lanza — mismo patrón de siempre."""
    payload = {"vehicle": id_sr_vehiculo, "driver": id_sr_piloto}
    ok, data = _sr_request("PATCH", f"routes/routes/{route_id}/", token=token, json=payload)
    if not ok:
        return False, data
    return True, "Vehículo/piloto reasignados en SimpliRoute."


def obtener_ruta_sr(route_id_sr, token=None):
    """GET /routes/routes/{route_id_sr}/ — trae el estado actual de la ruta,
    incluyendo `kilometers` y `total_distance` (pueden venir en None si SR
    todavía no lo calculó o si depende de un módulo que no tenemos activo —
    pendiente de confirmar empíricamente cuál de los dos se llena en la
    cuenta de Ransa) y `reference` (para verificar que el folio quedó
    guardado). También trae datos útiles para indicadores adicionales:
    carga utilizada, paradas totales, y horarios reales vs. planeados."""
    ok, data = _sr_request("GET", f"routes/routes/{route_id_sr}/", token=token)
    if not ok:
        return False, data
    ruta = data[0] if isinstance(data, list) else data
    return True, {
        "kilometers": ruta.get("kilometers"),
        "total_distance": ruta.get("total_distance"),
        "reference": ruta.get("reference"),
        "status": ruta.get("status"),
        "vehicle": ruta.get("vehicle"),
        "driver": ruta.get("driver"),
        "total_visits": ruta.get("total_visits"),
        "total_load": ruta.get("total_load"),
        "total_load_percentage": ruta.get("total_load_percentage"),
        "estimated_time_start": ruta.get("estimated_time_start"),
        "estimated_time_end": ruta.get("estimated_time_end"),
        "start_time": ruta.get("start_time"),
        "end_time": ruta.get("end_time"),
    }
