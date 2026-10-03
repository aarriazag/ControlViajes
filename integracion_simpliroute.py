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


# --- Placas: Control de Ruta guarda "896BYM" y SimpliRoute guarda "C-896BYM". Para COMPARARLAS se usa
# una clave que ignora el prefijo (C-, P-, TC-...): los 3 números + las 3 letras.
_PATRON_PLACA_GT = re.compile(r"^(?:[A-Z]{1,3})?(\d{3}[A-Z]{3})$")
_PATRON_PLACA_ESTANDAR = re.compile(r"^\d{3}[A-Z]{3}$")


def clave_placa(placa):
    """Clave para comparar placas sin importar mayúsculas, espacios, guiones ni el prefijo:
    '896BYM', 'C-896BYM', 'c 896 bym' y 'TC-896BYM' dan todas '896BYM'. Si la placa no sigue ese
    patrón (remolques, placas especiales) se compara tal cual, solo limpia."""
    if not placa:
        return ""
    limpio = re.sub(r"[^A-Z0-9]", "", str(placa).upper())
    m = _PATRON_PLACA_GT.match(limpio)
    return m.group(1) if m else limpio


def placa_para_sr(placa):
    """Formato con el que SimpliRoute guarda las placas: C-XXXBBB. Es lo que se manda a SR
    cuando hay que CREAR un vehículo allá."""
    k = clave_placa(placa)
    return "C-" + k if _PATRON_PLACA_ESTANDAR.match(k) else normalizar_placa(placa)


def placa_formato_local(placa):
    """Formato con el que Control de Ruta guarda las placas: XXXBBB (sin prefijo)."""
    k = clave_placa(placa)
    return k if _PATRON_PLACA_ESTANDAR.match(k) else normalizar_placa(placa)


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
    clave = clave_placa(placa)
    ok, data = _sr_request("GET", "routes/vehicles/", token=token)
    if not ok:
        return False, data
    lista = data.get("results", data) if isinstance(data, dict) else data
    for v in lista:
        if es_placa_dummy_sr(v.get("license_plate")):
            continue
        if clave_placa(v.get("license_plate")) == clave:   # "896BYM" (aquí) = "C-896BYM" (SR)
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
    placa = placa_para_sr(placa)   # SR guarda C-XXXBBB; aquí se guarda XXXBBB
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
    # Un vehículo que YA existe en SR (muchos los crea la integración de Infor) NO se renombra ni se
    # le cambia la placa: solo se le asigna su conductor por defecto, y solo si hay uno que asignar
    # (nunca se borra el que SR ya tenga).
    if not id_sr_piloto:
        return True, id_sr
    ok, data = _sr_request("PATCH", f"routes/vehicles/{id_sr}/", token=token, json={"default_driver": id_sr_piloto})
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


def debe_sincronizar(get_conn_fn, proceso, horas=12):
    """True si el proceso ('pilotos' o 'camiones') nunca se ha sincronizado, o si pasaron
    >= `horas` desde la última vez — para el disparo automático al abrir la app."""
    from contextlib import closing

    with closing(get_conn_fn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT ultima_ejecucion FROM control_sincronizacion_sr WHERE proceso = %s", (proceso,))
        fila = cur.fetchone()
    if not fila or not fila[0]:
        return True
    return (datetime.now() - fila[0]).total_seconds() >= horas * 3600


def debe_sincronizar_pilotos(get_conn_fn, horas=12):
    return debe_sincronizar(get_conn_fn, "pilotos", horas)


def debe_sincronizar_camiones(get_conn_fn, horas=12):
    return debe_sincronizar(get_conn_fn, "camiones", horas)


# ---------------------------------------------------------------------------
# CAMIONES — SR manda, Control de Ruta recibe (misma idea que los pilotos)
# ---------------------------------------------------------------------------

def obtener_vehiculos_sr(token=None):
    """GET /routes/vehicles/ — lista cruda de vehículos en SR."""
    ok, data = _sr_request("GET", "routes/vehicles/", token=token)
    if not ok:
        return False, data
    lista = data.get("results", data) if isinstance(data, dict) else data
    return True, list(lista)


def sincronizar_camiones_desde_sr(get_conn_fn, token=None):
    """Trae los vehículos de SR y los refleja en cat_camiones (para que cada camión real tenga su
    id_sr y se pueda prellenar al importar una Ruta). Las placas se comparan por su CLAVE (los 3
    números y las 3 letras), así "C-896BYM" de SR y "896BYM" de Control de Ruta son el mismo camión:
      - Vehículos "dummy" de planeación de Infor: se ignoran.
      - id_sr ya vinculado -> solo se marca la fecha de sincronización (la placa NO se renombra).
      - id_sr nuevo y la placa ya existe en el catálogo sin vínculo -> se vincula solo.
      - La placa existe pero vinculada a OTRO id_sr (vehículo repetido en SR) -> no se toca, se avisa.
      - No existe -> se crea INACTIVO y 'pendiente de completar', con la placa en formato local
        (sin "C-"); SR no trae transportista ni tipo de camión: un Administrador lo completa.
      - Auto-corrección: si una sincronización anterior (con el formato distinto) dejó un camión
        pendiente DUPLICADO ("C-896BYM") junto al camión real ("896BYM"), se fusionan: el duplicado
        se borra y el real queda vinculado.
    Devuelve (ok, resumen). Nunca lanza."""
    from contextlib import closing

    try:
        ok, vehiculos = obtener_vehiculos_sr(token=token)
        if not ok:
            return False, f"No se pudieron traer los vehículos de SimpliRoute: {vehiculos}"

        nuevos = vinculados = actualizados = dummies = fusionados = 0
        avisos, con_error = [], []
        with closing(get_conn_fn()) as conn, conn.cursor() as cur:
            for v in vehiculos:
                id_sr = v.get("id")
                placa_sr = v.get("license_plate") or v.get("name")
                if es_placa_dummy_sr(placa_sr):
                    dummies += 1
                    continue
                clave = clave_placa(placa_sr)
                cur.execute("SAVEPOINT sp_camion")
                try:
                    cur.execute("SELECT placa, id_sr, activo, COALESCE(pendiente_completar, FALSE) FROM cat_camiones")
                    todas = cur.fetchall()
                    por_id = next((f for f in todas if f[1] == id_sr), None)
                    misma_clave = [f for f in todas if clave_placa(f[0]) == clave]
                    if por_id:
                        gemelos = [f for f in misma_clave if f[0] != por_id[0] and f[1] is None]
                        if clave_placa(por_id[0]) != clave:
                            avisos.append(f"SR trae la placa {placa_sr} para el camión {por_id[0]} (no se renombró)")
                        if gemelos and por_id[3] and not por_id[2]:
                            # el pendiente creado antes con otro formato es un duplicado del camión real
                            cur.execute("DELETE FROM cat_camiones WHERE placa = %s", (por_id[0],))
                            cur.execute(
                                "UPDATE cat_camiones SET id_sr = %s, sincronizado_sr = TRUE, "
                                "fecha_sincronizacion_sr = %s WHERE placa = %s",
                                (id_sr, datetime.now(), gemelos[0][0]))
                            fusionados += 1
                        else:
                            cur.execute(
                                "UPDATE cat_camiones SET sincronizado_sr = TRUE, fecha_sincronizacion_sr = %s WHERE placa = %s",
                                (datetime.now(), por_id[0]))
                            actualizados += 1
                    else:
                        sin_vinculo = [f for f in misma_clave if f[1] is None]
                        if sin_vinculo:
                            if len(sin_vinculo) > 1:
                                avisos.append(f"Hay {len(sin_vinculo)} camiones con la misma placa que {placa_sr} en el catálogo "
                                              f"({', '.join(f[0] for f in sin_vinculo)}): se vinculó {sin_vinculo[0][0]}")
                            cur.execute(
                                "UPDATE cat_camiones SET id_sr = %s, sincronizado_sr = TRUE, "
                                "fecha_sincronizacion_sr = %s WHERE placa = %s",
                                (id_sr, datetime.now(), sin_vinculo[0][0]))
                            vinculados += 1
                        elif misma_clave:
                            avisos.append(f"La placa {placa_sr} aparece en SR con otro id (vehículo repetido en SR) — no se tocó")
                        else:
                            cur.execute(
                                "INSERT INTO cat_camiones (placa, activo, id_sr, sincronizado_sr, "
                                "fecha_sincronizacion_sr, pendiente_completar) "
                                "VALUES (%s, FALSE, %s, TRUE, %s, TRUE) ON CONFLICT (placa) DO NOTHING",
                                (placa_formato_local(placa_sr), id_sr, datetime.now()))
                            nuevos += 1
                    cur.execute("RELEASE SAVEPOINT sp_camion")
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_camion")
                    con_error.append((placa_sr, (str(e).splitlines() or ["error"])[0]))
            conn.commit()

            cur.execute(
                "INSERT INTO control_sincronizacion_sr (proceso, ultima_ejecucion, resultado) "
                "VALUES ('camiones', %s, %s) "
                "ON CONFLICT (proceso) DO UPDATE SET ultima_ejecucion = EXCLUDED.ultima_ejecucion, "
                "resultado = EXCLUDED.resultado",
                (datetime.now(), f"{nuevos} nuevo(s), {vinculados} vinculado(s) por placa, {fusionados} fusionado(s), "
                                 f"{actualizados} al día, {len(con_error)} con error"))
            conn.commit()

        resumen = (f"{nuevos} camión(es) nuevo(s) (pendientes de completar), {vinculados} vinculado(s) por placa, "
                   f"{actualizados} ya vinculado(s), {dummies} provisional(es) de Infor omitido(s).")
        if fusionados:
            resumen += f" {fusionados} duplicado(s) pendiente(s) fusionado(s) con su camión real."
        if avisos:
            resumen += " Avisos: " + "; ".join(avisos[:3]) + ("…" if len(avisos) > 3 else ".")
        if con_error:
            resumen += (f" {len(con_error)} sin procesar: "
                        + "; ".join(f"{p}: {m}" for p, m in con_error[:3]) + ("…" if len(con_error) > 3 else "."))
        return True, resumen
    except Exception as e:
        return False, f"Error inesperado al sincronizar camiones desde SR: {e}"


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

    # Placas y nombres legibles (solo para MOSTRAR en pantalla). Si estas dos consultas fallan, la
    # importación sigue igual, solo que se muestran los ids en vez de placa y nombre.
    placas_sr, nombres_sr = {}, {}
    try:
        ok_v, vehs = obtener_vehiculos_sr(token=token)
        if ok_v:
            placas_sr = {v.get("id"): (v.get("license_plate") or v.get("name")) for v in vehs}
        ok_d, drvs = obtener_drivers_sr(token=token)
        if ok_d:
            nombres_sr = {d.get("id"): (d.get("name") or d.get("username")) for d in drvs}
    except Exception:
        pass

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
            "vehicle_placa_sr": placas_sr.get(ruta.get("vehicle")),
            "driver_nombre_sr": nombres_sr.get(ruta.get("driver")),
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
        cur.execute("ALTER TABLE cat_camiones ADD COLUMN IF NOT EXISTS pendiente_completar BOOLEAN DEFAULT FALSE")
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
            CREATE TABLE IF NOT EXISTS sr_visit_types_vistos (
                visit_type TEXT PRIMARY KEY,
                primera_vez TIMESTAMP DEFAULT NOW(),
                ultima_vez TIMESTAMP DEFAULT NOW()
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


def preparar_auxiliar_libre(cur, texto):
    """El Auxiliar se escribe libre en el viaje. Esto lo deja listo para guardarse:
      - vacío -> "Sin Auxiliar";
      - con formato Mayúscula Inicial (o el nombre que YA existe, sin distinguir mayúsculas);
      - si es nuevo, se agrega a Catálogos → Auxiliares (activo) para que aparezca como
        sugerencia la próxima vez. Va dentro de la MISMA transacción del viaje: si el viaje
        no se guarda, el auxiliar tampoco. Devuelve el nombre final. Puede lanzar ValueError
        (nombre demasiado largo) o un error de base de datos; quien lo llama ya lo maneja."""
    nombre = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not nombre:
        nombre = "Sin Auxiliar"
    if len(nombre) > 100:
        raise ValueError("El nombre del auxiliar es demasiado largo (máximo 100 caracteres).")
    nombre = formatear_valores_catalogo(cur, "cat_auxiliares", {"nombre": nombre})["nombre"]
    cur.execute("INSERT INTO cat_auxiliares (nombre, activo) VALUES (%s, TRUE) ON CONFLICT (nombre) DO NOTHING", (nombre,))
    return nombre


def resolver_ruta_para_formulario(get_conn_fn, ruta):
    """Al usar una Ruta importada de SR: decide qué camión y piloto prellenar en el formulario.
    Devuelve {"placa": str|None, "piloto": str|None, "sin_piloto": bool, "avisos": [str]}.
      - Camión: se prellena si el vehículo de SR es un camión REAL, ya está en el catálogo (por id_sr,
        o por placa — en ese caso se vincula de paso) y está ACTIVO. Si es el provisional (dummy) de
        Infor, no se prellena y no se avisa: es lo normal, se elige el real.
      - Piloto: si SR no tiene conductor asignado, queda EN BLANCO (sin_piloto=True). Si lo tiene y
        está activo en el catálogo, se prellena. Si está pendiente/inactivo o no existe, se deja en
        blanco y se avisa por qué.
    Nunca lanza: ante cualquier fallo devuelve todo vacío y el digitador elige a mano."""
    from contextlib import closing

    r = {"placa": None, "piloto": None, "sin_piloto": ruta.get("driver_sr_id") is None, "avisos": []}
    try:
        with closing(get_conn_fn()) as conn, conn.cursor() as cur:
            # ---- Camión ----
            id_veh, placa_sr = ruta.get("vehicle_sr_id"), ruta.get("vehicle_placa_sr")
            if id_veh is not None and not (placa_sr is not None and es_placa_dummy_sr(placa_sr)):
                cur.execute("SELECT placa, activo FROM cat_camiones WHERE id_sr = %s", (id_veh,))
                fila = cur.fetchone()
                if not fila and placa_sr:
                    # "C-896BYM" en SR = "896BYM" en Control de Ruta (se compara por la clave de la placa)
                    cur.execute("SELECT placa, activo, id_sr FROM cat_camiones")
                    clave = clave_placa(placa_sr)
                    por_placa = next((f for f in cur.fetchall() if clave_placa(f[0]) == clave), None)
                    if por_placa:
                        if por_placa[2] is None:
                            cur.execute("UPDATE cat_camiones SET id_sr = %s, sincronizado_sr = TRUE, "
                                        "fecha_sincronizacion_sr = %s WHERE placa = %s",
                                        (id_veh, datetime.now(), por_placa[0]))
                            conn.commit()
                        fila = (por_placa[0], por_placa[1])
                if fila and fila[1]:
                    r["placa"] = fila[0]
                elif fila:
                    r["avisos"].append(f"El camión {fila[0]} de la Ruta está pendiente de completar o inactivo en "
                                       "Catálogos → Camiones: complétalo y actívalo para poder usarlo.")
                else:
                    r["avisos"].append(f"El vehículo {placa_sr or id_veh} de la Ruta no está en el catálogo de camiones: "
                                       "sincroniza camiones desde SimpliRoute (Catálogos → Integración SimpliRoute).")
            # ---- Piloto ----
            id_drv = ruta.get("driver_sr_id")
            if id_drv is not None:
                cur.execute("SELECT nombre, activo FROM cat_pilotos WHERE id_sr = %s", (id_drv,))
                fila = cur.fetchone()
                nombre_sr = ruta.get("driver_nombre_sr") or id_drv
                if fila and fila[1]:
                    r["piloto"] = fila[0]
                elif fila:
                    r["avisos"].append(f"El piloto {fila[0]} de la Ruta está pendiente de completar o inactivo en "
                                       "Catálogos → Pilotos: complétalo y actívalo para poder usarlo.")
                else:
                    r["avisos"].append(f"El piloto {nombre_sr} de la Ruta no está en el catálogo: "
                                       "sincroniza pilotos desde SimpliRoute.")
    except Exception:
        pass
    return r


def rutas_ya_utilizadas(get_conn_fn, route_ids):
    """Para la lista de Rutas importadas: cuáles YA tienen un viaje en Control de Ruta.
    Devuelve {route_id: {"viajes": [(id_viaje, estado), ...], "anulados": [id_viaje, ...]}} solo para
    las Rutas que ya se usaron. Un viaje ANULADO libera su Ruta (se puede volver a usar): por eso se
    separan "viajes" (activos o liquidados) de "anulados". Nunca lanza: si falla, devuelve {} y la lista
    se muestra como antes."""
    from contextlib import closing

    try:
        ids = [str(r) for r in route_ids if r]
        if not ids:
            return {}
        with closing(get_conn_fn()) as conn:
            _asegurar_tabla_vinculo(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s.route_id_sr, s.id_viaje, v.estado FROM sr_vinculo_viaje s "
                    "JOIN viajes v ON v.id_viaje = s.id_viaje WHERE s.route_id_sr = ANY(%s) ORDER BY s.id_viaje",
                    (ids,))
                filas = cur.fetchall()
        usadas = {}
        for route_id, id_viaje, estado in filas:
            d = usadas.setdefault(route_id, {"viajes": [], "anulados": []})
            if estado == "Anulado":
                d["anulados"].append(id_viaje)
            else:
                d["viajes"].append((id_viaje, estado))
        return usadas
    except Exception:
        return {}


def ruta_en_uso(get_conn_fn, route_id):
    """Candado al GUARDAR: devuelve (id_viaje, estado) si esa Ruta ya tiene un viaje no anulado, o None.
    Cubre el caso de dos personas con la misma Ruta abierta a la vez, o una lista que se quedó vieja en
    pantalla. Nunca lanza."""
    d = rutas_ya_utilizadas(get_conn_fn, [route_id]).get(str(route_id))
    if d and d["viajes"]:
        return d["viajes"][0]
    return None


def estado_vinculo_viaje(get_conn_fn, id_viaje):
    """Estado del vínculo de un viaje con su Ruta de SR, o None si no tiene. Nunca lanza."""
    from contextlib import closing
    try:
        with closing(get_conn_fn()) as conn:
            _asegurar_tabla_vinculo(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT route_id_sr, estado, ultimo_evento, ultimo_intento, ultimo_mensaje "
                            "FROM sr_vinculo_viaje WHERE id_viaje = %s", (id_viaje,))
                f = cur.fetchone()
        if not f:
            return None
        return {"route_id": f[0], "estado": f[1], "evento": f[2], "intento": f[3], "mensaje": f[4]}
    except Exception:
        return None


def vincular_viaje_con_ruta(get_conn_fn, id_viaje, route_id, token=None):
    """Vincula un viaje YA guardado con una Ruta de SR y le envía su camión y piloto. Sirve para viajes
    creados a mano o antes de que existiera el vínculo. Devuelve (ok, mensaje). Nunca lanza."""
    from contextlib import closing
    try:
        usada = ruta_en_uso(get_conn_fn, route_id)
        if usada and usada[0] != id_viaje:
            return False, f"Esa Ruta ya se utilizó en el viaje {usada[0]} ({usada[1]})."
        with closing(get_conn_fn()) as conn:
            _asegurar_tabla_vinculo(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sr_vinculo_viaje (id_viaje, route_id_sr, ultimo_evento, ultimo_intento, estado) "
                    "VALUES (%s, %s, 'vinculado', NOW(), 'vinculado') "
                    "ON CONFLICT (id_viaje) DO UPDATE SET route_id_sr = EXCLUDED.route_id_sr",
                    (id_viaje, str(route_id)))
            conn.commit()
        ok, msg = reenviar_viaje(get_conn_fn, id_viaje, token=token, evento_txt="vinculado")
        return ok, (f"Viaje vinculado con la Ruta. {msg}" if ok else f"Viaje vinculado con la Ruta, pero: {msg}")
    except Exception as e:
        return False, f"No se pudo vincular: {e}"


def render_panel_ruta_sr(st, get_conn_fn, viaje, token=None):
    """Recuadro «Ruta de SimpliRoute» dentro de Gestión de Viajes → editar. Muestra si el viaje está
    vinculado y qué pasó la última vez que se envió algo a SR (por eso una edición puede NO viajar), deja
    reenviar con un clic y, si el viaje no está vinculado, permite vincularlo a una Ruta de SR del día.
    Nunca rompe la pantalla de edición."""
    try:
        _render_panel_ruta_sr(st, get_conn_fn, viaje, token)
    except Exception as e:
        st.caption(f"(El recuadro de SimpliRoute no pudo cargarse: {e})")


def _render_panel_ruta_sr(st, get_conn_fn, viaje, token):
    from contextlib import closing
    if not SR_CONTROL_ACTIVO:
        return
    id_viaje = viaje["id_viaje"]
    est = estado_vinculo_viaje(get_conn_fn, id_viaje)
    with st.container(border=True):
        st.markdown("##### :material/sync: Ruta de SimpliRoute")
        if est:
            icono = {"ok": "✅", "error": "⚠️"}.get(est["estado"], "ℹ️")
            cuando = est["intento"].strftime("%d/%m %H:%M") if est["intento"] else "—"
            st.caption(f"Vinculado a la Ruta `{str(est['route_id'])[:8]}…` · último envío {icono} "
                       f"{est['estado'] or 'sin envíos'} ({est['evento'] or '—'}, {cuando})")
            if est["mensaje"]:
                (st.warning if est["estado"] == "error" else st.caption)(est["mensaje"])
            st.caption("Al guardar una corrección con otro camión o piloto, el cambio se envía a SimpliRoute solo. "
                       "Este botón envía lo que está GUARDADO en el viaje (no lo que estés escribiendo y aún no guardaste).")
            if st.button(":material/send: Reenviar camión y piloto a SimpliRoute ahora", key=f"panel_sr_reenviar_{id_viaje}"):
                ok, msg = reenviar_viaje(get_conn_fn, id_viaje, token=token, evento_txt="reenvio")
                (st.success if ok else st.error)(msg)
            return

        st.warning("Este viaje **no está vinculado** a una Ruta de SimpliRoute, por eso un cambio de camión o piloto "
                   "aquí NO se envía. Si es un viaje manual, no hace falta nada; si debió venir de una Ruta, vincúlalo.")
        with st.expander(":material/link: Vincular con una Ruta de SimpliRoute", expanded=False):
            with closing(get_conn_fn()) as conn, conn.cursor() as cur:
                cur.execute("SELECT cliente, fecha_creacion, placa FROM viajes WHERE id_viaje = %s", (id_viaje,))
                cliente, fecha_viaje, placa_viaje = cur.fetchone()
                cur.execute("SELECT visit_type FROM cat_mapeo_cliente_sr WHERE cliente = %s", (cliente,))
                tipos_cliente = {r[0] for r in cur.fetchall()}
            token = token or obtener_token_sr_desde_vault(get_conn_fn)
            if not token:
                st.error("No hay token de SimpliRoute configurado en el Vault.")
                return
            try:
                fecha_ini = datetime.strptime(str(fecha_viaje)[:10], "%Y-%m-%d").date()
            except Exception:
                fecha_ini = datetime.now().date()
            fecha = st.date_input("Fecha de la Ruta", value=fecha_ini, key=f"panel_sr_fecha_{id_viaje}")
            clave_rutas = f"panel_sr_rutas_{id_viaje}"
            if st.button(":material/search: Buscar Rutas de ese día", key=f"panel_sr_buscar_{id_viaje}"):
                ok, rutas = importar_rutas_sr(str(fecha), token=token)
                if not ok:
                    st.error(rutas)
                else:
                    st.session_state[clave_rutas] = rutas
            rutas = st.session_state.get(clave_rutas)
            if rutas is None:
                return
            usadas = rutas_ya_utilizadas(get_conn_fn, [r["route_id"] for r in rutas])
            libres = [r for r in rutas
                      if not usadas.get(str(r["route_id"]), {}).get("viajes")
                      and (not tipos_cliente or set(r["visit_types"]) & tipos_cliente)]
            # primero las que traen el mismo camión del viaje
            libres.sort(key=lambda r: 0 if clave_placa(r.get("vehicle_placa_sr")) == clave_placa(placa_viaje) else 1)
            if not libres:
                st.info("No hay Rutas libres de este cliente ese día (las ya utilizadas no se pueden vincular).")
                return
            for i, r in enumerate(libres[:15]):
                tiendas = ", ".join(d["tienda"] for d in r["destinos"][:3]) + ("…" if len(r["destinos"]) > 3 else "")
                misma = "⭐ mismo camión · " if clave_placa(r.get("vehicle_placa_sr")) == clave_placa(placa_viaje) else ""
                st.write(f"{misma}**{r.get('vehicle_placa_sr') or r['vehicle_sr_id']}** · piloto "
                         f"{r.get('driver_nombre_sr') or 'sin asignar'} · {len(r['destinos'])} parada(s): {tiendas}")
                if st.button(":material/link: Vincular esta Ruta y enviar camión/piloto", key=f"panel_sr_vincular_{id_viaje}_{i}"):
                    ok, msg = vincular_viaje_con_ruta(get_conn_fn, id_viaje, r["route_id"], token=token)
                    st.session_state.pop(clave_rutas, None)
                    st.session_state["flash_sr" if not ok else "flash_sr_ok"] = msg
                    st.rerun()


# ---------------------------------------------------------------------------
# MAPEO visit_type de SR  ->  cliente de Control de Ruta
# ---------------------------------------------------------------------------
# El visit_type es un texto libre que viene en las Visitas de SR. Para que el mapeo no dependa de
# escribirlo a mano (y de equivocarse), solo se pueden mapear los visit_type que SR YA HA ENVIADO:
# se anotan en sr_visit_types_vistos cada vez que se consultan Rutas, o al "Actualizar la lista".

def registrar_visit_types_vistos(get_conn_fn, tipos):
    """Anota los visit_type que SR acaba de enviar. Nunca lanza."""
    from contextlib import closing
    try:
        tipos = {str(x).strip() for x in tipos if x and str(x).strip() and str(x).strip() != "(sin tipo)"}
        if not tipos:
            return
        with closing(get_conn_fn()) as conn:
            _asegurar_esquema_visit_types(conn)
            with conn.cursor() as cur:
                for x in tipos:
                    cur.execute(
                        "INSERT INTO sr_visit_types_vistos (visit_type) VALUES (%s) "
                        "ON CONFLICT (visit_type) DO UPDATE SET ultima_vez = NOW()", (x,))
            conn.commit()
    except Exception:
        pass


def _asegurar_esquema_visit_types(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS sr_visit_types_vistos (visit_type TEXT PRIMARY KEY, "
                    "primera_vez TIMESTAMP DEFAULT NOW(), ultima_vez TIMESTAMP DEFAULT NOW())")
        cur.execute("CREATE TABLE IF NOT EXISTS cat_mapeo_cliente_sr (visit_type TEXT PRIMARY KEY, cliente TEXT)")
    conn.commit()


def escanear_visit_types_sr(get_conn_fn, token=None, dias=7):
    """Recorre las Visitas de SR de los últimos `dias` días (y de mañana) y anota los visit_type que
    encuentra. Devuelve (ok, resumen). Nunca lanza."""
    from datetime import timedelta
    try:
        hoy = datetime.now().date()
        fechas = [hoy + timedelta(days=d) for d in range(-int(dias), 2)]
        encontrados, dias_ok, dias_fallo = set(), 0, 0
        for f in fechas:
            ok, raw = _sr_request("GET", "routes/visits/", token=token, timeout=45, params={"planned_date": str(f)})
            if not ok:
                dias_fallo += 1
                continue
            dias_ok += 1
            visitas = raw.get("results", raw) if isinstance(raw, dict) else raw
            encontrados |= {v.get("visit_type") for v in visitas if v.get("visit_type")}
        if dias_ok == 0:
            return False, "No se pudo consultar SimpliRoute en ninguno de los días."
        antes = {x for x, _ in visit_types_vistos(get_conn_fn)}
        registrar_visit_types_vistos(get_conn_fn, encontrados)
        nuevos = len({x for x in encontrados if x and x not in antes})
        resumen = f"Se revisaron {dias_ok} día(s) de SimpliRoute: {len(encontrados)} visit_type encontrado(s), {nuevos} nuevo(s) en la lista."
        if dias_fallo:
            resumen += f" {dias_fallo} día(s) no respondieron."
        return True, resumen
    except Exception as e:
        return False, f"Error inesperado al revisar SimpliRoute: {e}"


def visit_types_vistos(get_conn_fn):
    """[(visit_type, ultima_vez)] de lo que SR ha enviado. Nunca lanza."""
    from contextlib import closing
    try:
        with closing(get_conn_fn()) as conn:
            _asegurar_esquema_visit_types(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT visit_type, ultima_vez FROM sr_visit_types_vistos ORDER BY visit_type")
                return cur.fetchall()
    except Exception:
        return []


def guardar_mapeo_visit_type(get_conn_fn, visit_type, cliente):
    """Mapea un visit_type a un cliente. SOLO acepta un visit_type que SR ya haya enviado."""
    from contextlib import closing
    try:
        visit_type = str(visit_type or "").strip()
        with closing(get_conn_fn()) as conn:
            _asegurar_esquema_visit_types(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM sr_visit_types_vistos WHERE visit_type = %s", (visit_type,))
                if not cur.fetchone():
                    return False, f"«{visit_type}» no es un visit_type que SimpliRoute haya enviado: elígelo de la lista."
                cur.execute("SELECT 1 FROM cat_clientes WHERE nombre = %s", (cliente,))
                if not cur.fetchone():
                    return False, f"El cliente «{cliente}» no existe en el catálogo."
                cur.execute("INSERT INTO cat_mapeo_cliente_sr (visit_type, cliente) VALUES (%s, %s) "
                            "ON CONFLICT (visit_type) DO UPDATE SET cliente = EXCLUDED.cliente", (visit_type, cliente))
            conn.commit()
        return True, f"«{visit_type}» quedó asignado a {cliente}."
    except Exception as e:
        return False, f"No se pudo guardar: {e}"


def aplicar_cambios_mapeo(get_conn_fn, cambios_cliente, borrar):
    """cambios_cliente: {visit_type: cliente_nuevo}; borrar: [visit_type]. Todo o nada. Devuelve (ok, mensaje)."""
    from contextlib import closing
    try:
        with closing(get_conn_fn()) as conn:
            with conn.cursor() as cur:
                for vt in borrar:
                    cur.execute("DELETE FROM cat_mapeo_cliente_sr WHERE visit_type = %s", (vt,))
                for vt, cli in cambios_cliente.items():
                    if vt in borrar:
                        continue
                    cur.execute("SELECT 1 FROM cat_clientes WHERE nombre = %s", (cli,))
                    if not cur.fetchone():
                        conn.rollback()
                        return False, f"El cliente «{cli}» no existe en el catálogo."
                    cur.execute("UPDATE cat_mapeo_cliente_sr SET cliente = %s WHERE visit_type = %s", (cli, vt))
            conn.commit()
        partes = []
        if borrar:
            partes.append(f"{len(borrar)} borrado(s)")
        cambiados = [vt for vt in cambios_cliente if vt not in borrar]
        if cambiados:
            partes.append(f"{len(cambiados)} reasignado(s)")
        return True, "Cambios guardados: " + (", ".join(partes) if partes else "ninguno.")
    except Exception as e:
        return False, f"No se pudieron guardar los cambios: {e}"


def render_mapeo_visit_type(st, get_conn_fn, clientes, token=None):
    """Pantalla del mapeo visit_type de SR -> cliente (Catálogos → Integración SimpliRoute). Se puede
    CORREGIR el cliente y BORRAR cualquier mapeo; al agregar uno nuevo solo se puede elegir un visit_type
    que SR ya haya enviado. Nunca rompe el resto de la pantalla."""
    try:
        _render_mapeo_visit_type(st, get_conn_fn, list(clientes), token)
    except Exception as e:
        st.error(f"⚠️ El mapeo no pudo cargarse: {e}. El resto de la app no se afecta.")


def _render_mapeo_visit_type(st, get_conn_fn, clientes, token):
    from contextlib import closing
    with closing(get_conn_fn()) as conn:
        _asegurar_esquema_visit_types(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT visit_type, cliente FROM cat_mapeo_cliente_sr ORDER BY cliente, visit_type")
            mapeos = cur.fetchall()
    vistos = {x for x, _ in visit_types_vistos(get_conn_fn)}

    st.caption("Un cliente puede tener varios visit_type en SR. Un visit_type sin mapear queda «sin cliente identificado» al "
               "importar — nunca se asume solo. Aquí solo se pueden elegir visit_type que SimpliRoute ya envió.")

    # ---- Lo que ya está mapeado: corregir o borrar ----
    st.markdown("**Mapeos actuales**")
    if not mapeos:
        st.info("Todavía no hay ningún visit_type mapeado.")
    else:
        h1, h2, h3, h4 = st.columns([3, 1, 3, 1])
        h1.caption("visit_type"); h2.caption("¿Existe en SR?"); h3.caption("Cliente"); h4.caption("Borrar")
        cambios, borrar = {}, []
        for i, (vt, cli) in enumerate(mapeos):
            c1, c2, c3, c4 = st.columns([3, 1, 3, 1])
            c1.write(f"`{vt}`")
            c2.write("✅" if vt in vistos else "⚠️ no aparece")
            opciones = clientes if cli in clientes else clientes + [cli]
            nuevo = c3.selectbox("Cliente", opciones, index=opciones.index(cli), key=f"mapeo_cli_{i}", label_visibility="collapsed")
            if nuevo != cli:
                cambios[vt] = nuevo
            if c4.checkbox("Borrar", key=f"mapeo_del_{i}", label_visibility="collapsed"):
                borrar.append(vt)
        if any(vt not in vistos for vt, _ in mapeos):
            st.caption("⚠️ = SimpliRoute nunca ha enviado ese visit_type (puede ser un error de captura): corrígelo o bórralo. "
                       "Si es un visit_type nuevo, usa «Actualizar la lista» abajo antes de decidir.")
        if st.button(":material/save: Guardar cambios", key="mapeo_guardar_cambios", disabled=not (cambios or borrar)):
            ok, msg = aplicar_cambios_mapeo(get_conn_fn, cambios, borrar)
            st.session_state["flash_catalogos"] = ("success" if ok else "error", msg)
            st.rerun()

    # ---- Agregar uno nuevo: solo de la lista de SR ----
    st.markdown("**Agregar un visit_type**")
    ya_mapeados = {vt for vt, _ in mapeos}
    sin_mapear = sorted(vistos - ya_mapeados)
    if not token:
        st.warning("No hay token de SimpliRoute configurado: no se puede actualizar la lista.")
    ca, cb = st.columns([1, 2])
    dias = ca.number_input("Días a revisar", min_value=1, max_value=31, value=7, step=1, key="mapeo_dias_escaneo")
    cb.write(""); cb.write("")
    if token and cb.button(":material/sync: Actualizar la lista desde SimpliRoute", key="mapeo_escanear"):
        ok, msg = escanear_visit_types_sr(get_conn_fn, token=token, dias=int(dias))
        st.session_state["flash_catalogos"] = ("success" if ok else "error", msg)
        st.rerun()
    if not sin_mapear:
        st.info("No hay visit_type de SimpliRoute pendientes de mapear. Si esperas uno nuevo, pulsa «Actualizar la lista».")
        return
    d1, d2, d3 = st.columns([3, 3, 1])
    elegido = d1.selectbox("visit_type de SR", sin_mapear, key="mapeo_nuevo_vt")
    cliente = d2.selectbox("Cliente en Control de Ruta", clientes, key="mapeo_nuevo_cli") if clientes else None
    d3.write(""); d3.write("")
    if d3.button(":material/add: Agregar", key="mapeo_agregar", disabled=not cliente):
        ok, msg = guardar_mapeo_visit_type(get_conn_fn, elegido, cliente)
        st.session_state["flash_catalogos"] = ("success" if ok else "error", msg)
        st.rerun()


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
ENVIAR_CAMBIOS_A_SR = True    # True = al EDITAR un viaje (camión o piloto) el cambio se envía a SR, y se habilita
                              #        el botón "Reenviar" del Control SR. False = "observar": al editar no se
                              #        escribe nada en SR (solo se registra y el semáforo lo marca)
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


def reenviar_viaje(get_conn_fn, id_viaje, token=None, evento_txt="reenvio"):
    """Manda a SR el camión/piloto que Control de Ruta tiene AHORA para este viaje y confirma que quedó.
    Primero CONSULTA la Ruta en SR: si ya tiene ese camión y ese piloto, no escribe nada (editar, por
    ejemplo, las cajas de un destino no toca SR). Devuelve (ok, mensaje). Nunca lanza.
    Si el piloto elegido NO tiene vínculo con SR (id_sr), el cambio de piloto no puede enviarse: se
    devuelve ok=False con el motivo, para que no parezca que SR quedó igual que Control de Ruta."""
    try:
        d = _leer_vinculo_y_viaje(get_conn_fn, id_viaje)
        if not d:
            return False, "Este viaje no tiene una Ruta de SimpliRoute vinculada."
        if d["estado"] == "Anulado":
            return False, "El viaje está anulado en Control de Ruta — no se reenvía nada."
        if not d["id_sr_camion"]:
            msg = (f"El camión {d['placa']} no tiene id_sr — sincronízalo primero en "
                   "Catálogos → Integración SimpliRoute.")
            _registrar(get_conn_fn, id_viaje, evento_txt, "error", msg)
            return False, msg
        token = token or obtener_token_sr_desde_vault(get_conn_fn)
        if not token:
            msg = "No hay token de SimpliRoute configurado en el Vault."
            _registrar(get_conn_fn, id_viaje, evento_txt, "error", msg)
            return False, msg

        piloto_sin_vinculo = bool(d["piloto"]) and d["piloto"] != "Sin Piloto" and not d["id_sr_piloto"]
        aviso_piloto = (f"El piloto {d['piloto']} no tiene vínculo con SimpliRoute (id_sr): el cambio de piloto NO se "
                        "envió. Sincroniza los pilotos desde SimpliRoute y vuelve a intentarlo.")

        ok_g, ruta = obtener_ruta_sr(d["route_id"], token=token)
        if not ok_g:
            msg = f"No se pudo consultar la Ruta en SimpliRoute: {ruta}"
            _registrar(get_conn_fn, id_viaje, evento_txt, "error", msg)
            return False, msg
        camion_igual = str(ruta.get("vehicle")) == str(d["id_sr_camion"])
        piloto_igual = (not d["id_sr_piloto"]) or str(ruta.get("driver")) == str(d["id_sr_piloto"])
        if camion_igual and piloto_igual:
            if d["id_sr_piloto"]:
                ok, msg = True, "SimpliRoute ya tenía este camión y este piloto: no hubo nada que enviar."
            else:
                ok, msg = True, ("El camión ya coincide en SimpliRoute. Sin piloto asignado no se envía nada: "
                                 "SimpliRoute conserva el conductor que ya tenga.")
        else:
            ok, msg = reasignar_vehiculo_piloto_ruta_sr(
                d["route_id"], d["id_sr_camion"], d["id_sr_piloto"], token=token
            )
        if ok and piloto_sin_vinculo:
            ok, msg = False, aviso_piloto
        _registrar(get_conn_fn, id_viaje, evento_txt, "ok" if ok else "error", msg)
        return ok, msg
    except Exception as e:
        return False, f"Error inesperado al reenviar a SimpliRoute: {e}"


def evento(get_conn_fn, tipo, id_viaje, route_id_sr=None, token=None, cambio_camion_piloto=None):
    """Único punto de entrada desde app.py. `tipo`: 'viaje_guardado',
    'viaje_editado' o 'viaje_anulado'. Devuelve (ok, mensaje): app.py muestra el mensaje
    (en verde si ok=True, en amarillo si ok=False); mensaje vacío = nada que decir. Nunca lanza."""
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
                ok, msg = reenviar_viaje(get_conn_fn, id_viaje, token=token, evento_txt="guardado")
                if not ok:
                    return False, (f"El viaje {id_viaje} se guardó bien, pero no se pudo actualizar el "
                                   f"vehículo/piloto en SimpliRoute: {msg}")
                return True, f"Viaje {id_viaje}: {msg}"
            return True, ""

        if tipo == "viaje_editado":
            if not _leer_vinculo_y_viaje(get_conn_fn, id_viaje):
                # Un viaje manual no tiene Ruta de SR: editarlo en silencio es lo normal. Pero si se cambió el
                # camión o el piloto, hay que decir por qué NO llegó a SimpliRoute (antes se callaba).
                if cambio_camion_piloto and ENVIAR_CAMBIOS_A_SR:
                    return False, (f"El viaje {id_viaje} no está vinculado a una Ruta de SimpliRoute, por eso el cambio de "
                                   "camión/piloto NO se envió. Vincúlalo en el recuadro «Ruta de SimpliRoute» de esta pantalla.")
                return True, ""
            if ENVIAR_CAMBIOS_A_SR:
                ok, msg = reenviar_viaje(get_conn_fn, id_viaje, token=token, evento_txt="editado")
                if not ok:
                    return False, (f"El viaje {id_viaje} se corrigió bien, pero el cambio no llegó a "
                                   f"SimpliRoute: {msg}")
                return True, f"Viaje {id_viaje}: {msg}"
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
