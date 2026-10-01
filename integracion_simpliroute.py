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
import time
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


def _sr_request(method, ruta, token=None, timeout=None, reintentos_429=2, **kwargs):
    """Wrapper único para todas las llamadas a la API de SR.

    Devuelve siempre (ok: bool, data_o_mensaje). Nunca lanza una excepción:
    - Error de red / timeout -> (False, mensaje corto y claro)
    - HTTP 429 (demasiadas peticiones) -> espera y reintenta sola, hasta
      `reintentos_429` veces, respetando el header Retry-After de SR si lo
      manda; si se agotan los reintentos, mensaje claro (no un error crudo).
    - HTTP 4xx/5xx            -> (False, mensaje con el código y el body de SR)
    - HTTP 2xx                -> (True, json ya parseado)

    `timeout` es opcional — por defecto usa TIMEOUT_SEGUNDOS (8s), pensado
    para llamadas ligeras (un vehículo, un piloto). Las que traen listas
    completas de un día entero (ej. importar_rutas_sr) piden explícitamente
    un timeout más largo, porque la respuesta puede ser de miles de
    registros y 8s se queda corto — no es un error, es una respuesta grande
    de verdad."""
    url = BASE_URL + ruta.lstrip("/")
    for intento in range(reintentos_429 + 1):
        try:
            resp = requests.request(
                method, url, headers=_headers(token), timeout=(timeout or TIMEOUT_SEGUNDOS), **kwargs
            )
        except requests.exceptions.Timeout:
            return False, f"SimpliRoute no respondió a tiempo (timeout de {timeout or TIMEOUT_SEGUNDOS}s)."
        except requests.exceptions.RequestException as e:
            return False, f"No se pudo conectar con SimpliRoute: {e}"

        if resp.status_code == 429 and intento < reintentos_429:
            espera = resp.headers.get("Retry-After")
            try:
                espera = float(espera)
            except (TypeError, ValueError):
                espera = 3 * (intento + 1)  # 3s, luego 6s si hace falta un segundo reintento
            time.sleep(min(espera, 15))  # nunca más de 15s de espera, para no colgar la pantalla
            continue
        break

    if resp.status_code == 429:
        return False, ("SimpliRoute está limitando las peticiones por exceso de consultas seguidas "
                        "(código 429). Espera uno o dos minutos y vuelve a intentar — no es un error "
                        "de tu base de datos ni de Control de Ruta.")

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


# Formato de nombres de catálogo (pilotos, transportistas, tiendas...).
CONECTORES_NOMBRE = {"de", "del", "la", "las", "los", "y", "e", "da", "do", "van", "von"}
# Siglas de respaldo, SOLO si no se puede leer la tabla cat_siglas. La lista de verdad vive en
# Catálogos → Siglas (la puede editar un Administrador sin tocar el código).
SIGLAS_NOMBRE = {"S.A.", "S.A", "SA", "SAS", "S.A.S.", "CD", "CEDI", "KFC", "IVA", "TM", "II", "III", "IV", "DHL", "UPS"}


def _es_sigla(token, siglas):
    """¿Esta palabra es una sigla que debe ir TODA en mayúsculas?
    Sí si: (1) está en la lista de siglas; (2) son letras separadas por puntos (S.A., S.R.L.);
    (3) es una palabra corta de hasta 4 letras SIN vocales (MYM, SRL, LTDA, JC); o (4) empieza con
    número y trae letras (10TM, 5T, 4X4)."""
    base = token.strip(",")
    if not base:
        return False
    if base.upper() in siglas:
        return True
    if re.fullmatch(r"[A-Za-z](\.[A-Za-z])+\.?", base):
        return True
    if re.fullmatch(r"[A-Za-z]{1,4}", base) and not re.search(r"[aeiou]", base, re.I) and base.lower() != "y":
        return True
    if re.fullmatch(r"[0-9][0-9A-Za-z]*", base) and re.search(r"[A-Za-z]", base):
        return True
    return False


def normalizar_nombre_catalogo(valor, modo="entidad", siglas=None):
    """Da un formato uniforme (Mayúscula Inicial) a un nombre de catálogo.

    modo="persona" (pilotos, auxiliares): SIEMPRE Mayúscula Inicial; los conectores
      (de, del, la, los, y...) quedan en minúscula.
    modo="entidad" (clientes, transportistas, tiendas, CDs...): lo que viene TODO en mayúsculas o TODO
      en minúsculas pasa a Mayúscula Inicial; si ya trae formato propio (ej. "UniSuper Importados")
      se respeta, salvo que las siglas se ponen siempre en mayúsculas ("Transportes Mym" -> "Transportes MYM").
    `siglas`: conjunto de siglas (de Catálogos → Siglas). Si no se da, se usa la lista de respaldo.
    En ambos: se quitan espacios sobrantes y se respetan los acentos. Nunca lanza."""
    try:
        if valor is None:
            return valor
        siglas = SIGLAS_NOMBRE if siglas is None else siglas
        s = re.sub(r"\s+", " ", str(valor)).strip()
        s = re.sub(r"\s+,", ",", s)
        if not s:
            return s
        # "(SR 123)" / "(SR)" al final lo pone el sistema al separar pilotos homónimos: se respeta tal cual
        sufijo = ""
        m_suf = re.search(r"\s\(SR(?: [0-9]+)?\)$", s)
        if m_suf:
            sufijo = m_suf.group(0)
            s = s[:m_suf.start()]
        partes = s.split(" ")
        if modo == "entidad" and any(c.isupper() for c in s) and any(c.islower() for c in s):
            return " ".join(p.upper() if _es_sigla(p, siglas) else p for p in partes) + sufijo
        palabras = []
        for i, p in enumerate(partes):
            siguiente = partes[i + 1] if i + 1 < len(partes) else ""
            if _es_sigla(p, siglas):
                palabras.append(p.upper())
            elif i > 0 and p.lower() in CONECTORES_NOMBRE and not siguiente[:1].isdigit():
                # (un conector seguido de número es parte del nombre: "Los 4", "Las 3 Marías")
                palabras.append(p.lower())
            else:
                palabras.append("-".join(t[:1].upper() + t[1:].lower() for t in p.split("-")))
        return " ".join(palabras) + sufijo
    except Exception:
        return valor


def _cargar_siglas(cur):
    """Lee Catálogos → Siglas. Devuelve un conjunto en mayúsculas, o None si no se pudo leer
    (en ese caso se usa la lista de respaldo). Aislado en un SAVEPOINT para que un fallo aquí
    nunca arruine la operación de guardado que está en curso."""
    try:
        cur.execute("SAVEPOINT sp_siglas")
        cur.execute("SELECT sigla FROM cat_siglas")
        lista = {str(r[0]).strip().upper() for r in cur.fetchall() if r[0]}
        cur.execute("RELEASE SAVEPOINT sp_siglas")
        return lista
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sp_siglas")
        except Exception:
            pass
        return None


def asegurar_esquema_formato(conn):
    """Crea Catálogos → Siglas (tabla cat_siglas) la primera vez y la llena con las siglas
    comunes. Si ya existe, no toca nada (así lo que se borre a propósito no reaparece)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.cat_siglas')")
        if cur.fetchone()[0] is None:
            cur.execute("CREATE TABLE cat_siglas (sigla TEXT PRIMARY KEY)")
            for s in sorted(SIGLAS_NOMBRE):
                cur.execute("INSERT INTO cat_siglas (sigla) VALUES (%s) ON CONFLICT DO NOTHING", (s,))
    conn.commit()


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


def obtener_token_sr_desde_vault(get_conn_fn):
    """Lee el token de SimpliRoute desde Supabase Vault (guardado ahí con
    `select vault.create_secret(...)`) en vez de st.secrets — así el token
    vive centralizado en la base de datos, no repartido entre Render y
    GitHub. Las credenciales de Postgres NO pueden vivir aquí (se necesitan
    para poder conectarse a la base en primer lugar) — esas siguen viniendo
    de Render. Devuelve el token (str) o None si no está configurado."""
    from contextlib import closing
    try:
        with closing(get_conn_fn()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = %s",
                ("simpliroute_api_token",)
            )
            fila = cur.fetchone()
            return fila[0] if fila else None
    except Exception:
        return None  # Vault no configurado, sin permisos, o la tabla no existe todavía


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

    nuevos, actualizados, renombrados, en_revision, con_error = 0, 0, 0, 0, []
    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        siglas = _cargar_siglas(cur)
        for d in drivers:
            id_sr = d.get("id")
            nombre_raw = (d.get("name") or "").strip() or d.get("username", f"Piloto SR {id_sr}")
            # Mismo formato que la captura manual, ANTES de comparar nada: así
            # SR puede mandar "JUAN PEREZ" y en Control de Ruta sigue siendo
            # "Juan Perez". La vinculación es siempre por id_sr, nunca por nombre.
            nombre_sr = normalizar_nombre_catalogo(nombre_raw, "persona", siglas)
            cur.execute("SAVEPOINT sp_piloto")  # un fallo en un piloto no debe abortar a los demás
            try:
                cur.execute("SELECT nombre FROM cat_pilotos WHERE id_sr = %s", (id_sr,))
                fila = cur.fetchone()
                if fila:
                    nombre_actual = fila[0]
                    if nombre_actual == f"{nombre_sr} (SR {id_sr})":
                        # Homónimo que esta misma sincronización creó aparte a propósito:
                        # no se vuelve a intentar renombrar (evitaría avisar en cada corrida).
                        cur.execute("UPDATE cat_pilotos SET fecha_sincronizacion_sr = %s WHERE id_sr = %s",
                                    (datetime.now(), id_sr))
                        actualizados += 1
                        cur.execute("RELEASE SAVEPOINT sp_piloto")
                        continue
                    if nombre_actual != nombre_sr:
                        # ¿otro piloto (otra persona) ya se llama así? Entonces NO se renombra.
                        cur.execute(
                            "SELECT 1 FROM cat_pilotos WHERE lower(btrim(nombre)) = lower(%s) "
                            "AND id_sr IS DISTINCT FROM %s",
                            (nombre_sr, id_sr)
                        )
                        if cur.fetchone():
                            con_error.append((id_sr, f"'{nombre_sr}' ya lo usa otro piloto — no se renombró '{nombre_actual}'"))
                            cur.execute("UPDATE cat_pilotos SET fecha_sincronizacion_sr = %s WHERE id_sr = %s",
                                        (datetime.now(), id_sr))
                            cur.execute("RELEASE SAVEPOINT sp_piloto")
                            continue
                        renombrados += 1
                    cur.execute(
                        "UPDATE cat_pilotos SET nombre = %s, fecha_sincronizacion_sr = %s WHERE id_sr = %s",
                        (nombre_sr, datetime.now(), id_sr)
                    )
                    actualizados += 1
                    cur.execute("RELEASE SAVEPOINT sp_piloto")
                    continue

                # id_sr nuevo — ¿ya hay un piloto local con ese nombre (sin distinguir mayúsculas)?
                cur.execute(
                    "SELECT nombre, id_sr FROM cat_pilotos WHERE lower(btrim(nombre)) = lower(%s)",
                    (nombre_sr,)
                )
                choque = cur.fetchone()
                if choque and choque[1] is None:
                    # Mismo nombre que un piloto local SIN vincular: no se fusiona solo
                    # (podrían ser dos personas). Se guarda el nombre LOCAL para que los
                    # botones de la cola de revisión encuentren a ese piloto.
                    cur.execute(
                        "INSERT INTO cat_pilotos_revision_nombre (nombre, id_sr, fecha_deteccion) "
                        "VALUES (%s, %s, %s) ON CONFLICT (id_sr) DO NOTHING",
                        (choque[0], id_sr, datetime.now())
                    )
                    en_revision += 1
                else:
                    # Sin choque, o el nombre ya pertenece a un piloto vinculado a OTRO id_sr
                    # (dos personas con el mismo nombre): se crea pendiente, con el id_sr al
                    # final del nombre para no pisar ni fusionar con el otro.
                    nombre_nuevo = nombre_sr if not choque else f"{nombre_sr} (SR {id_sr})"
                    cur.execute(
                        "INSERT INTO cat_pilotos (nombre, id_sr, activo, pendiente_completar, "
                        "fecha_sincronizacion_sr) VALUES (%s, %s, FALSE, TRUE, %s) "
                        "ON CONFLICT (nombre) DO NOTHING",
                        (nombre_nuevo, id_sr, datetime.now())
                    )
                    nuevos += 1
                cur.execute("RELEASE SAVEPOINT sp_piloto")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_piloto")
                con_error.append((id_sr, (str(e).splitlines() or ["error"])[0]))
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
               f"{actualizados} actualizado(s) ({renombrados} con el nombre ajustado), "
               f"{en_revision} en revisión por nombre duplicado.")
    if con_error:
        resumen += (f" {len(con_error)} sin procesar: "
                    + "; ".join(f"id_sr {i}: {m}" for i, m in con_error[:3])
                    + ("…" if len(con_error) > 3 else "."))
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
            {"tienda": str, "cajas": int, "orden_sugerido": int, "pedidos": [{"pedido": reference, "cajas": n, "visit_id": id}]}
        ],  # "destinos" ya viene ORDENADO según el optimizador de SR (campo
            # `order` de sus Visitas) — es un punto de partida sugerido, no
            # definitivo; en Control de Ruta se puede reordenar con las
            # flechitas antes de guardar el viaje.
      }
    """
    ok, rutas_raw = _sr_request("GET", "routes/routes/", token=token, timeout=45, params={"planned_date": fecha})
    if not ok:
        return False, f"No se pudieron traer las Rutas de SimpliRoute: {rutas_raw}"
    lista_rutas = rutas_raw.get("results", rutas_raw) if isinstance(rutas_raw, dict) else rutas_raw

    ok, visitas_raw = _sr_request("GET", "routes/visits/", token=token, timeout=45, params={"planned_date": fecha})
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
                por_tienda[tienda] = {"tienda": tienda, "cajas": 0, "pedidos": [], "orden_sugerido": None}
            cajas_visita = v.get("load_3") or 0
            por_tienda[tienda]["cajas"] += cajas_visita
            por_tienda[tienda]["pedidos"].append({
                "pedido": v.get("reference"),
                "cajas": cajas_visita,
                "visit_id": v.get("id"),
                "origen": "SR",
            })
            # El orden más bajo entre todas las visitas de esta tienda —
            # SR numera cada parada según su optimizador; una tienda con
            # varias visitas (varios pedidos) se ubica donde llegaría la
            # primera de ellas.
            orden_visita = v.get("order")
            if orden_visita is not None:
                actual = por_tienda[tienda]["orden_sugerido"]
                if actual is None or orden_visita < actual:
                    por_tienda[tienda]["orden_sugerido"] = orden_visita

        destinos_ordenados = sorted(
            por_tienda.values(),
            key=lambda d: d["orden_sugerido"] if d["orden_sugerido"] is not None else float("inf")
        )

        resultado.append({
            "route_id": route_id,
            "vehicle_sr_id": ruta.get("vehicle"),
            "driver_sr_id": ruta.get("driver"),
            "total_visitas": len(visitas_de_ruta),
            "visit_types": visit_types,
            "destinos": destinos_ordenados,
        })

    return True, resultado


def reasignar_vehiculo_piloto_ruta_sr(route_id, id_sr_vehiculo, id_sr_piloto, token=None):
    """PATCH a una Ruta existente para reemplazar el vehículo/piloto (ej. el
    dummy de Infor) por el camión/piloto real que se asignó en Control de
    Ruta. Nunca lanza — mismo patrón de siempre.

    Dos protecciones sobre la versión anterior:
      - Si el piloto NO tiene id_sr, ya no se manda `driver` (antes se mandaba
        null, y SR podía dejar la Ruta sin piloto). Solo se cambia el vehículo.
      - Después del PATCH se consulta la Ruta de nuevo para CONFIRMAR que el
        vehículo (y el piloto, si se mandó) quedaron como se pidió — que SR
        responda 200 no garantiza que el cambio haya quedado."""
    payload = {"vehicle": id_sr_vehiculo}
    if id_sr_piloto:
        payload["driver"] = id_sr_piloto
    ok, data = _sr_request("PATCH", f"routes/routes/{route_id}/", token=token, json=payload)
    if not ok:
        return False, data

    ok_v, ruta = obtener_ruta_sr(route_id, token=token)
    if not ok_v:
        return True, f"Enviado a SimpliRoute, pero no se pudo confirmar con una consulta posterior: {ruta}"
    if str(ruta.get("vehicle")) != str(id_sr_vehiculo):
        return False, "SimpliRoute respondió que sí, pero la Ruta sigue con otro vehículo — el cambio no quedó."
    if id_sr_piloto and str(ruta.get("driver")) != str(id_sr_piloto):
        return False, "SimpliRoute respondió que sí, pero la Ruta sigue con otro piloto — el cambio no quedó."
    if id_sr_piloto:
        return True, "Vehículo y piloto reasignados y confirmados en SimpliRoute."
    return True, "Vehículo reasignado y confirmado en SimpliRoute (el piloto no se tocó: no tiene id_sr)."


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



def asegurar_esquema_sr(conn):
    """Crea (si no existen) las tablas y columnas que usa la integración con
    SimpliRoute. Antes vivían solo como migraciones manuales en la base, así
    que una base nueva o vaciada no las tenía. Todo es IF NOT EXISTS: en una
    base que ya las tiene no cambia nada. Lo llama init_db() de app.py."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE cat_pilotos ADD COLUMN IF NOT EXISTS id_sr BIGINT")
        cur.execute("ALTER TABLE cat_pilotos ADD COLUMN IF NOT EXISTS pendiente_completar BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE cat_pilotos ADD COLUMN IF NOT EXISTS fecha_sincronizacion_sr TIMESTAMP")
        cur.execute("ALTER TABLE cat_camiones ADD COLUMN IF NOT EXISTS id_sr BIGINT")
        cur.execute("ALTER TABLE cat_camiones ADD COLUMN IF NOT EXISTS sincronizado_sr BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE cat_camiones ADD COLUMN IF NOT EXISTS fecha_sincronizacion_sr TIMESTAMP")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_pilotos_revision_nombre (
                nombre TEXT, id_sr BIGINT PRIMARY KEY,
                fecha_deteccion TIMESTAMP, resuelto BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS control_sincronizacion_sr (
                proceso TEXT PRIMARY KEY, ultima_ejecucion TIMESTAMP, resultado TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_mapeo_cliente_sr (
                visit_type TEXT PRIMARY KEY, cliente TEXT
            )
        """)
    conn.commit()


# ---------------------------------------------------------------------------
# Formato de nombres al GUARDAR en catálogos (formulario, tabla en pantalla y Excel)
# ---------------------------------------------------------------------------
# Cada catálogo dice qué columnas son nombres, de qué tipo (persona/entidad) y contra
# qué se comparan para reutilizar el nombre que YA existe (sin distinguir mayúsculas):
#   ("tabla", "columna")            -> nombre existente en esa tabla (ej. el cliente de una tienda
#                                      se ajusta al nombre que ya tiene cat_clientes)
#   ("tabla", "columna", "cliente") -> igual, pero solo entre filas del mismo cliente (tiendas, CDs)
#   None                            -> solo se le da formato, no se compara con nada
# Un nombre que YA existe se respeta tal cual está (no se pelea con lo que hay en la base);
# solo a lo NUEVO se le da formato. Lo ya existente se corrige con el SQL de formato de nombres.
FORMATO_CATALOGOS = {
    "cat_clientes":           [("nombre", "entidad", ("cat_clientes", "nombre"))],
    "cat_transportistas":     [("nombre", "entidad", ("cat_transportistas", "nombre")),
                               ("razon_social", "entidad", None)],
    "cat_pilotos":            [("nombre", "persona", ("cat_pilotos", "nombre"))],
    "cat_auxiliares":         [("nombre", "persona", ("cat_auxiliares", "nombre"))],
    "cat_camiones":           [("transportista", "entidad", ("cat_transportistas", "nombre")),
                               ("piloto", "persona", ("cat_pilotos", "nombre")),
                               ("auxiliar", "persona", ("cat_auxiliares", "nombre"))],
    "cat_clientes_tiendas":   [("cliente", "entidad", ("cat_clientes", "nombre")),
                               ("tienda", "entidad", ("cat_clientes_tiendas", "tienda", "cliente"))],
    "cat_cds_por_cliente":    [("cliente", "entidad", ("cat_clientes", "nombre")),
                               ("cd", "entidad", ("cat_cds_por_cliente", "cd", "cliente"))],
    "cat_motivos_sin_pedido": [("nombre", "entidad", ("cat_motivos_sin_pedido", "nombre"))],
    "cat_usuario_clientes":   [("cliente", "entidad", ("cat_clientes", "nombre"))],
    "cat_siglas":             [("sigla", "sigla", ("cat_siglas", "sigla"))],
}


def _clave_nombre(v):
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def _mapa_existentes(cur, fuente):
    """{clave sin mayúsculas: nombre como está guardado}; con 3er elemento en `fuente`,
    la clave es (cliente, nombre) para comparar solo dentro del mismo cliente."""
    if fuente is None:
        return {}
    tabla, col = fuente[0], fuente[1]
    if len(fuente) == 3:
        cur.execute(f"SELECT {fuente[2]}, {col} FROM {tabla}")
        return {(_clave_nombre(a), _clave_nombre(b)): b for a, b in cur.fetchall() if a is not None and b is not None}
    cur.execute(f"SELECT {col} FROM {tabla}")
    return {_clave_nombre(a): a for (a,) in cur.fetchall() if a is not None}


def _formatear_un_valor(valor, modo, mapa, alcance=None, siglas=None):
    if valor is None or valor != valor:          # None o NaN
        return valor
    s = str(valor).strip()
    if not s:
        return valor
    clave = (alcance, _clave_nombre(s)) if alcance is not None else _clave_nombre(s)
    if clave in mapa:
        return mapa[clave]                        # ya existe: se usa tal cual está guardado
    if modo == "sigla":
        return s.upper()                          # las siglas siempre se guardan en mayúsculas
    return normalizar_nombre_catalogo(s, modo, siglas)


def formatear_df_catalogo(cur, tabla, df):
    """Devuelve una COPIA del DataFrame con los nombres ya formateados. Nunca lanza:
    si algo fallara, devuelve el DataFrame original y el guardado sigue como antes."""
    try:
        especs = FORMATO_CATALOGOS.get(tabla)
        if not especs:
            return df
        df = df.copy()
        siglas = _cargar_siglas(cur)
        for col, modo, fuente in especs:
            if col not in df.columns:
                continue
            mapa = _mapa_existentes(cur, fuente)
            col_alcance = fuente[2] if (fuente and len(fuente) == 3) else None
            if col_alcance and col_alcance in df.columns:
                df[col] = [_formatear_un_valor(v, modo, mapa, _clave_nombre(a), siglas)
                           for v, a in zip(df[col], df[col_alcance])]
            else:
                df[col] = [_formatear_un_valor(v, modo, mapa, None, siglas) for v in df[col]]
        return df
    except Exception:
        return df


def formatear_valores_catalogo(cur, tabla, valores):
    """Igual que formatear_df_catalogo, para un solo registro (dict del formulario)."""
    try:
        especs = FORMATO_CATALOGOS.get(tabla)
        if not especs:
            return valores
        nuevos = dict(valores)
        siglas = _cargar_siglas(cur)
        for col, modo, fuente in especs:
            if col not in nuevos:
                continue
            mapa = _mapa_existentes(cur, fuente)
            alcance = None
            if fuente and len(fuente) == 3 and fuente[2] in nuevos and nuevos[fuente[2]] is not None:
                alcance = _clave_nombre(nuevos[fuente[2]])
            nuevos[col] = _formatear_un_valor(nuevos[col], modo, mapa, alcance, siglas)
        return nuevos
    except Exception:
        return valores


# ===========================================================================
# CONTROL SR — vínculo viaje <-> Ruta, verificación y reenvío
# ===========================================================================
# Todo el "control" vive aquí. app.py solo tiene un puente delgado
# (puente_sr) que llama a evento() y a render_control_sr(); cualquier ajuste
# futuro se hace en este archivo sin volver a tocar app.py.
#
# INTERRUPTORES (se cambian aquí, sin tocar app.py):
SR_CONTROL_ACTIVO = True      # False = apaga TODO este bloque (la app sigue igual)
ENVIAR_AL_GUARDAR = True      # True = mismo comportamiento que ya tenía la app: al guardar
                              #        un viaje importado de SR, manda camión/piloto reales a SR
ENVIAR_CAMBIOS_A_SR = False   # False = "observar": al EDITAR un viaje no se escribe nada en SR
                              #        (solo se registra y el semáforo lo marca). True = al editar
                              #        se reenvía camión/piloto, y se habilita el botón "Reenviar"
MAX_VERIFICACIONES = 40       # tope de Rutas consultadas por clic (cuida el límite 429 de SR)

from contextlib import closing

_TABLA_VINCULO_LISTA = False


def _asegurar_tabla_vinculo(conn):
    """Crea la tabla propia del control la primera vez. No toca `viajes`."""
    global _TABLA_VINCULO_LISTA
    if _TABLA_VINCULO_LISTA:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sr_vinculo_viaje (
                id_viaje TEXT PRIMARY KEY,
                route_id_sr TEXT NOT NULL,
                creado TIMESTAMP DEFAULT NOW(),
                ultimo_evento TEXT,
                ultimo_intento TIMESTAMP,
                estado TEXT,
                ultimo_mensaje TEXT
            )
        """)
    conn.commit()
    _TABLA_VINCULO_LISTA = True


def _registrar(get_conn_fn, id_viaje, evento_txt, estado, mensaje):
    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE sr_vinculo_viaje SET ultimo_evento=%s, ultimo_intento=NOW(), estado=%s, ultimo_mensaje=%s "
            "WHERE id_viaje=%s",
            (evento_txt, estado, (mensaje or "")[:500], id_viaje)
        )
        conn.commit()


def _leer_vinculo_y_viaje(get_conn_fn, id_viaje):
    """Devuelve dict con route_id, placa, piloto, estado del viaje e ids de SR
    del camión/piloto — o None si el viaje no tiene Ruta vinculada."""
    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.route_id_sr, v.placa, v.piloto, v.estado FROM sr_vinculo_viaje s "
            "JOIN viajes v ON v.id_viaje = s.id_viaje WHERE s.id_viaje = %s", (id_viaje,)
        )
        fila = cur.fetchone()
        if not fila:
            return None
        route_id, placa, piloto, estado = fila
        cur.execute("SELECT id_sr FROM cat_camiones WHERE placa = %s", (placa,))
        f = cur.fetchone()
        id_cam = f[0] if f else None
        id_pil = None
        if piloto:
            cur.execute("SELECT id_sr FROM cat_pilotos WHERE nombre = %s", (piloto,))
            f = cur.fetchone()
            id_pil = f[0] if f else None
    return {"route_id": route_id, "placa": placa, "piloto": piloto, "estado": estado,
            "id_sr_camion": id_cam, "id_sr_piloto": id_pil}


def reenviar_viaje(get_conn_fn, id_viaje, token=None):
    """Manda a SR el camión/piloto que Control de Ruta tiene AHORA para este
    viaje, y confirma que quedó. Devuelve (ok, mensaje). Nunca lanza."""
    try:
        d = _leer_vinculo_y_viaje(get_conn_fn, id_viaje)
        if not d:
            return False, "Este viaje no tiene una Ruta de SimpliRoute vinculada."
        if d["estado"] == "Anulado":
            return False, "El viaje está anulado en Control de Ruta — no se reenvía nada."
        if not d["id_sr_camion"]:
            return False, (f"El camión {d['placa']} no tiene id_sr — sincronízalo primero en "
                           "Catálogos → Integración SimpliRoute.")
        token = token or obtener_token_sr_desde_vault(get_conn_fn)
        if not token:
            return False, "No hay token de SimpliRoute configurado en el Vault."
        ok, msg = reasignar_vehiculo_piloto_ruta_sr(
            d["route_id"], d["id_sr_camion"], d["id_sr_piloto"], token=token
        )
        _registrar(get_conn_fn, id_viaje, "reenvio", "ok" if ok else "error", msg)
        return ok, msg
    except Exception as e:
        return False, f"Error inesperado al reenviar a SimpliRoute: {e}"


def evento(get_conn_fn, tipo, id_viaje, route_id_sr=None, token=None):
    """Único punto de entrada desde app.py. `tipo`: 'viaje_guardado',
    'viaje_editado' o 'viaje_anulado'. Devuelve (ok, mensaje): app.py solo
    muestra el mensaje si ok=False. Nunca lanza."""
    if not SR_CONTROL_ACTIVO:
        return True, ""
    try:
        with closing(get_conn_fn()) as conn:
            _asegurar_tabla_vinculo(conn)

        if tipo == "viaje_guardado":
            if not route_id_sr:
                return True, ""  # viaje creado a mano: no hay Ruta de SR que vincular
            with closing(get_conn_fn()) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sr_vinculo_viaje (id_viaje, route_id_sr, ultimo_evento, ultimo_intento, estado) "
                    "VALUES (%s, %s, 'guardado', NOW(), 'vinculado') "
                    "ON CONFLICT (id_viaje) DO UPDATE SET route_id_sr = EXCLUDED.route_id_sr",
                    (id_viaje, str(route_id_sr))
                )
                conn.commit()
            if ENVIAR_AL_GUARDAR:
                ok, msg = reenviar_viaje(get_conn_fn, id_viaje, token=token)
                if not ok:
                    return False, (f"El viaje {id_viaje} se guardó bien, pero no se pudo actualizar el "
                                   f"vehículo/piloto en SimpliRoute: {msg}")
            return True, ""

        if tipo == "viaje_editado":
            if not _leer_vinculo_y_viaje(get_conn_fn, id_viaje):
                return True, ""  # este viaje nunca tuvo Ruta de SR
            if ENVIAR_CAMBIOS_A_SR:
                ok, msg = reenviar_viaje(get_conn_fn, id_viaje, token=token)
                if not ok:
                    return False, (f"El viaje {id_viaje} se corrigió bien, pero el cambio no llegó a "
                                   f"SimpliRoute: {msg}")
            else:
                _registrar(get_conn_fn, id_viaje, "editado", "cambio_sin_enviar",
                           "Se editó en Control de Ruta; el envío a SR está en modo observar.")
            return True, ""

        if tipo == "viaje_anulado":
            if _leer_vinculo_y_viaje(get_conn_fn, id_viaje):
                _registrar(get_conn_fn, id_viaje, "anulado", "anulado_en_cr",
                           "Viaje anulado en Control de Ruta; la Ruta en SR no se toca.")
            return True, ""

        return True, ""
    except Exception as e:
        return False, f"Control SR: {e}"


def verificar_viaje(get_conn_fn, id_viaje, token=None):
    """Compara lo que Control de Ruta tiene del viaje contra la Ruta real en
    SR. Devuelve dict {id_viaje, route_id, semaforo, detalle, reenviable}.
    Solo LEE de SR. No compara cajas (son medidas distintas) ni estado de
    entrega (ahí manda SR)."""
    base = {"id_viaje": id_viaje, "route_id": None, "semaforo": "⚠️", "detalle": "", "reenviable": False}
    try:
        d = _leer_vinculo_y_viaje(get_conn_fn, id_viaje)
        if not d:
            base["detalle"] = "Sin Ruta de SR vinculada."
            return base
        base["route_id"] = d["route_id"]
        ok, ruta = obtener_ruta_sr(d["route_id"], token=token)
        if not ok:
            if "404" in str(ruta):
                base.update(semaforo="❌", detalle="La Ruta ya no existe en SimpliRoute.")
            else:
                base["detalle"] = f"No se pudo consultar SR: {ruta}"
            return base

        problemas = []
        cambio_reenviable = False
        if d["estado"] == "Anulado":
            problemas.append("Viaje anulado en Control de Ruta, pero la Ruta sigue viva en SR")
        else:
            if not d["id_sr_camion"]:
                problemas.append(f"El camión {d['placa']} no tiene id_sr")
            elif str(ruta.get("vehicle")) != str(d["id_sr_camion"]):
                problemas.append(f"SR tiene otro vehículo distinto a {d['placa']} (posible dummy de Infor)")
                cambio_reenviable = True
            if not d["id_sr_piloto"]:
                problemas.append(f"El piloto {d['piloto']} no tiene id_sr")
            elif str(ruta.get("driver")) != str(d["id_sr_piloto"]):
                problemas.append(f"SR tiene otro piloto distinto a {d['piloto']}")
                cambio_reenviable = True

        if problemas:
            base.update(semaforo="⚠️", detalle="; ".join(problemas),
                        reenviable=bool(cambio_reenviable and d["id_sr_camion"]))
        else:
            base.update(semaforo="✅", detalle="Camión y piloto coinciden con SR.")
        return base
    except Exception as e:
        base["detalle"] = f"Error inesperado al verificar: {e}"
        return base


def render_control_sr(st, get_conn_fn, token=None):
    """Pantalla 'Control SR' (Administrador/SuperAdministrador/Supervisor).
    Recibe `st` como parámetro para que el módulo siga sin depender de
    Streamlit al importarse. Cualquier fallo se muestra aquí adentro y nunca
    afecta al resto de la pantalla."""
    try:
        _render_control_sr(st, get_conn_fn, token)
    except Exception as e:
        st.error(f"⚠️ El Control SR no pudo cargarse: {e}. El resto de la app no se afecta.")


def _render_control_sr(st, get_conn_fn, token):
    if not SR_CONTROL_ACTIVO:
        st.info("El Control SR está apagado (SR_CONTROL_ACTIVO = False en integracion_simpliroute.py).")
        return

    with closing(get_conn_fn()) as conn:
        _asegurar_tabla_vinculo(conn)

    modo = "ENVIAR cambios a SR" if ENVIAR_CAMBIOS_A_SR else "OBSERVAR (al editar no se escribe nada en SR)"
    st.caption(f"Modo actual: **{modo}**. Compara camión y piloto del viaje contra la Ruta real en SimpliRoute. "
               "No compara cajas ni estado de entrega.")

    fecha = st.date_input("Fecha de los viajes", value=datetime.now().date(), key="ctl_sr_fecha")
    fecha_txt = str(fecha)

    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT v.id_viaje, v.cliente, v.placa, v.piloto, v.estado, s.estado, s.ultimo_mensaje "
            "FROM viajes v JOIN sr_vinculo_viaje s ON s.id_viaje = v.id_viaje "
            "WHERE v.fecha_creacion = %s ORDER BY v.id_viaje", (fecha_txt,)
        )
        filas = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) FROM viajes v WHERE v.fecha_creacion = %s AND v.estado != 'Anulado' "
            "AND NOT EXISTS (SELECT 1 FROM sr_vinculo_viaje s WHERE s.id_viaje = v.id_viaje)", (fecha_txt,)
        )
        sin_vinculo = cur.fetchone()[0]

    if sin_vinculo:
        st.caption(f"{sin_vinculo} viaje(s) de ese día no tienen Ruta de SR vinculada (creados a mano, "
                   "o antes de activar este control) — no se pueden verificar.")
    if not filas:
        st.info("No hay viajes con Ruta de SR vinculada en esa fecha.")
        return

    st.dataframe(
        [{"Viaje": f[0], "Cliente": f[1], "Camión": f[2], "Piloto": f[3], "Estado viaje": f[4],
          "Último envío a SR": f[5] or "", "Detalle": f[6] or ""} for f in filas],
        use_container_width=True, hide_index=True
    )

    token = token or obtener_token_sr_desde_vault(get_conn_fn)
    if not token:
        st.warning("⚠️ No hay token de SimpliRoute en el Vault — no se puede verificar contra SR.")
        return

    if st.button(":material/fact_check: Verificar contra SimpliRoute", key="ctl_sr_verificar"):
        resultados = []
        lote = filas[:MAX_VERIFICACIONES]
        barra = st.progress(0.0)
        for i, f in enumerate(lote):
            resultados.append(verificar_viaje(get_conn_fn, f[0], token=token))
            barra.progress((i + 1) / len(lote))
        barra.empty()
        st.session_state["ctl_sr_resultado"] = {"fecha": fecha_txt, "filas": resultados,
                                                "recortado": len(filas) > MAX_VERIFICACIONES}
        st.rerun()

    res = st.session_state.get("ctl_sr_resultado")
    if res and res["fecha"] == fecha_txt:
        if res["recortado"]:
            st.caption(f"Se verificaron solo los primeros {MAX_VERIFICACIONES} viajes (para no saturar a SR).")
        st.dataframe(
            [{"": r["semaforo"], "Viaje": r["id_viaje"], "Ruta SR": r["route_id"] or "", "Resultado": r["detalle"]}
             for r in res["filas"]],
            use_container_width=True, hide_index=True
        )
        pendientes = [r for r in res["filas"] if r["reenviable"]]
        if pendientes and not ENVIAR_CAMBIOS_A_SR:
            st.caption("Hay diferencias que se podrían corregir con 'Reenviar', pero el modo actual es OBSERVAR: "
                       "el botón se habilita al poner ENVIAR_CAMBIOS_A_SR = True en integracion_simpliroute.py.")
        elif pendientes:
            for r in pendientes:
                if st.button(f":material/send: Reenviar {r['id_viaje']} a SR", key=f"ctl_sr_reenviar_{r['id_viaje']}"):
                    ok, msg = reenviar_viaje(get_conn_fn, r["id_viaje"], token=token)
                    st.session_state.pop("ctl_sr_resultado", None)
                    (st.success if ok else st.error)(f"{r['id_viaje']}: {msg}")
