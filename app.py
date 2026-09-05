import streamlit as st
import pandas as pd
import psycopg2
import psycopg2.extras
from datetime import datetime
from contextlib import closing

# Configuración de la página web con estilo e identidad corporativa
st.set_page_config(page_title="Ransa - Gestión Logística de Viajes", layout="wide", page_icon="🚚")

# --- ESTILOS VISUALES RANSA (VERDE Y BLANCO) ---
st.markdown("""
    <style>
        div.stButton > button:first-child {
            background-color: #007A33 !important;
            color: white !important;
            border-radius: 6px !important;
            border: none !important;
            font-weight: bold !important;
        }
        div.stButton > button:first-child:hover {
            background-color: #005C25 !important;
            color: white !important;
        }
        button[data-baseweb="tab"] {
            font-size: 16px !important;
            font-weight: bold !important;
        }
        button[aria-selected="true"] {
            color: #007A33 !important;
            border-bottom-color: #007A33 !important;
        }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# CAPA DE BASE DE DATOS (Postgres / Supabase)
# Reemplaza session_state y SQLite: los datos viven en Supabase, así que
# sobreviven a reinicios del servidor y se comparten entre todos los usuarios
# (despachador y liquidador ven la misma información en tiempo real).
#
# Config necesaria en .streamlit/secrets.toml (nunca subir este archivo a GitHub):
#   [postgres]
#   host = "db.xxxxxxxxxxxx.supabase.co"
#   port = 5432
#   dbname = "postgres"
#   user = "postgres"
#   password = "..."
# ==========================================

def get_conn():
    cfg = st.secrets["postgres"]
    return psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"]
    )


def init_db():
    """Crea las tablas si no existen todavía. Se puede correr las veces que sea:
    no borra ni duplica nada si las tablas ya están creadas."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contadores (
                cliente TEXT PRIMARY KEY,
                ultimo_correlativo INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS viajes (
                id SERIAL PRIMARY KEY,
                id_viaje TEXT UNIQUE NOT NULL,
                cliente TEXT NOT NULL,
                placa TEXT NOT NULL,
                transportista TEXT,
                piloto TEXT,
                auxiliar TEXT,
                usuario_creador TEXT,
                fecha_creacion TEXT,
                hora_creacion TEXT,
                estado TEXT DEFAULT 'Pendiente de Liquidar'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS destinos (
                id SERIAL PRIMARY KEY,
                viaje_id INTEGER NOT NULL REFERENCES viajes(id),
                orden INTEGER,
                tienda TEXT,
                km REAL,
                galones_base REAL,
                pedidos TEXT,
                marchamo_ida TEXT UNIQUE NOT NULL,
                marchamo_regreso TEXT,
                roles INTEGER DEFAULT 0,
                tarimas INTEGER DEFAULT 0,
                cajas INTEGER DEFAULT 0
            )
        """)
        # Columnas de liquidación (se agregan solas si no existen; no afecta datos ya guardados)
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS roles_devueltos INTEGER")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS tarimas_devueltas INTEGER")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS pacas_carton_devueltas INTEGER")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS usuario_liquido TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS fecha_liquidacion TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS hora_liquidacion TEXT")
        conn.commit()


init_db()


def marchamo_ya_usado(marchamo, cur):
    cur.execute("SELECT 1 FROM destinos WHERE marchamo_ida = %s", (marchamo,))
    return cur.fetchone() is not None


def peek_siguiente_correlativo(cliente):
    """Solo para mostrar una vista previa en pantalla; NO reserva el número."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT ultimo_correlativo FROM contadores WHERE cliente = %s", (cliente,))
        row = cur.fetchone()
        numero = (row[0] if row else 0) + 1
    prefijo = "".join([w[0] for w in cliente.split()]).upper()[:4]
    return f"{prefijo}-{numero:04d}"


def siguiente_correlativo(cliente, cur):
    """Incrementa de forma atómica el contador del cliente y devuelve el No. de Viaje.
    Al estar dentro de la misma transacción que el INSERT del viaje, Postgres bloquea
    la fila del contador hasta que se confirme, así que dos digitadores guardando al
    mismo tiempo NUNCA reciben el mismo número."""
    cur.execute(
        "INSERT INTO contadores (cliente, ultimo_correlativo) VALUES (%s, 0) "
        "ON CONFLICT (cliente) DO NOTHING", (cliente,)
    )
    cur.execute(
        "UPDATE contadores SET ultimo_correlativo = ultimo_correlativo + 1 "
        "WHERE cliente = %s RETURNING ultimo_correlativo", (cliente,)
    )
    numero = cur.fetchone()[0]
    prefijo = "".join([w[0] for w in cliente.split()]).upper()[:4]
    return f"{prefijo}-{numero:04d}"


def guardar_viaje(cliente, placa, transportista, piloto, auxiliar, usuario, destinos_viaje):
    """Guarda el viaje y sus destinos en una sola transacción.
    Devuelve (True, id_viaje) si funcionó, o (False, mensaje_error) si no."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                # Revalidar marchamos DENTRO de la transacción (evita condiciones de
                # carrera entre dos digitadores guardando al mismo tiempo)
                for dest in destinos_viaje:
                    if marchamo_ya_usado(dest["marchamo_ida"], cur):
                        conn.rollback()
                        return False, f"El marchamo '{dest['marchamo_ida']}' ya fue usado en otro viaje."

                id_viaje_str = siguiente_correlativo(cliente, cur)
                fecha_hoy = datetime.now().strftime("%Y-%m-%d")
                hora_hoy = datetime.now().strftime("%H:%M:%S")

                cur.execute(
                    "INSERT INTO viajes (id_viaje, cliente, placa, transportista, piloto, auxiliar, "
                    "usuario_creador, fecha_creacion, hora_creacion) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING id",
                    (id_viaje_str, cliente, placa, transportista, piloto, auxiliar, usuario, fecha_hoy, hora_hoy)
                )
                viaje_id = cur.fetchone()[0]

                for i, dest in enumerate(destinos_viaje):
                    cur.execute(
                        "INSERT INTO destinos (viaje_id, orden, tienda, km, galones_base, pedidos, "
                        "marchamo_ida, marchamo_regreso, roles, tarimas, cajas) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (viaje_id, i + 1, dest["tienda"], dest["km"], dest["galones_base"], dest["pedidos"],
                         dest["marchamo_ida"], dest["marchamo_regreso"] or None,
                         dest["roles"], dest["tarimas"], dest["cajas"])
                    )
            conn.commit()
            return True, id_viaje_str
        except psycopg2.IntegrityError as e:
            conn.rollback()
            return False, f"Error de integridad (probablemente un marchamo duplicado): {e}"


def obtener_viajes_recientes(limite=10):
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            "SELECT id_viaje, cliente, placa, piloto, fecha_creacion, hora_creacion, estado "
            "FROM viajes ORDER BY id DESC LIMIT %s", conn, params=(limite,)
        )


def buscar_viaje_para_liquidar(valor_busqueda):
    """Busca un viaje por No. de Viaje, Marchamo de Ida (de cualquiera de sus destinos), o Placa.
    Devuelve (viaje, destinos) o (None, []) si no encuentra nada."""
    valor = valor_busqueda.strip()
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT v.* FROM viajes v
            WHERE v.id_viaje = %s OR v.placa = %s
               OR EXISTS (SELECT 1 FROM destinos d WHERE d.viaje_id = v.id AND d.marchamo_ida = %s)
            ORDER BY v.id DESC LIMIT 1
        """, (valor, valor, valor))
        viaje = cur.fetchone()
        if not viaje:
            return None, []
        cur.execute("SELECT * FROM destinos WHERE viaje_id = %s ORDER BY orden", (viaje["id"],))
        destinos = cur.fetchall()
        return viaje, destinos


def liquidar_viaje(viaje_id, destinos_actualizados, usuario):
    """Registra lo que el camión trajo de regreso por cada destino y marca el viaje como Liquidado.
    destinos_actualizados: lista de dicts con id, roles_devueltos, tarimas_devueltas, pacas_carton_devueltas."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                for d in destinos_actualizados:
                    cur.execute(
                        "UPDATE destinos SET roles_devueltos=%s, tarimas_devueltas=%s, "
                        "pacas_carton_devueltas=%s WHERE id=%s",
                        (d["roles_devueltos"], d["tarimas_devueltas"], d["pacas_carton_devueltas"], d["id"])
                    )
                fecha_hoy = datetime.now().strftime("%Y-%m-%d")
                hora_hoy = datetime.now().strftime("%H:%M:%S")
                cur.execute(
                    "UPDATE viajes SET estado='Liquidado', usuario_liquido=%s, "
                    "fecha_liquidacion=%s, hora_liquidacion=%s WHERE id=%s",
                    (usuario, fecha_hoy, hora_hoy, viaje_id)
                )
            conn.commit()
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


# ==========================================
# CATÁLOGOS MAESTROS (por ahora en memoria; candidatos a moverse a
# tablas propias más adelante si se necesita editarlos desde la app)
# ==========================================
if 'catalogos' not in st.session_state:
    st.session_state.catalogos = {
        "usuarios": {
            "Admin_Logistica": "Administrador",
            "Op_Salidas": "Operador",
            "Liq_Transporte": "Liquidador"
        },
        "transportistas": ["Transportes Express", "Logística del Norte", "Flota Interna"],
        "clientes": {
            "Supermercados Alfa": {
                "Alfa Central": {"km": 15.5, "galones_base": 3.5},
                "Alfa Norte": {"km": 32.0, "galones_base": 7.0},
                "Alfa Sur": {"km": 8.4, "galones_base": 2.0}
            },
            "Hiper Tiendas Beta": {
                "Beta Este": {"km": 22.1, "galones_base": 5.0},
                "Beta Oeste": {"km": 45.3, "galones_base": 10.0}
            }
        },
        "camiones": {
            "C-123ABC": {"tipo": "5 Ton", "transportista": "Transportes Express", "piloto": "Juan Pérez", "auxiliar": "Carlos López"},
            "C-456DEF": {"tipo": "10 Ton", "transportista": "Logística del Norte", "piloto": "María Rodríguez", "auxiliar": "Pedro Gómez"},
            "C-789GHI": {"tipo": "20 Ton", "transportista": "Flota Interna", "piloto": "Luis Martínez", "auxiliar": "José Hernández"}
        },
        "pilotos": ["Juan Pérez", "María Rodríguez", "Luis Martínez", "Andrés Custodio"],
        "auxiliares": ["Carlos López", "Pedro Gómez", "José Hernández", "Ramiro Ruiz"]
    }

# Contador de "corridas" del formulario: se incrementa después de guardar un
# viaje para que los widgets nazcan con keys nuevas (así el formulario queda
# limpio para el siguiente viaje, en vez de arrastrar lo digitado antes).
if 'form_run' not in st.session_state:
    st.session_state.form_run = 0
if 'num_destinos' not in st.session_state:
    st.session_state.num_destinos = 1

# ==========================================
# SIDEBAR
# ==========================================
st.sidebar.markdown("<h2 style='color:#007A33; font-weight:bold;'>RANSA</h2>", unsafe_allow_html=True)
st.sidebar.title("🔐 Configuración Inicial")

cliente_activo = st.sidebar.selectbox("🎯 CLIENTE ACTIVO", list(st.session_state.catalogos["clientes"].keys()))

st.sidebar.markdown("---")
try:
    with closing(get_conn()):
        pass
    st.sidebar.success("🟢 Conectado a la base de datos")
except Exception as e:
    st.sidebar.error(f"🔴 Sin conexión a la base de datos: {e}")
    st.stop()

st.sidebar.markdown("---")
usuario_activo = st.sidebar.selectbox("Simular Usuario Activo", list(st.session_state.catalogos["usuarios"].keys()))
perfil_activo = st.session_state.catalogos["usuarios"][usuario_activo]
st.sidebar.info(f"**Perfil:** {perfil_activo}")
st.sidebar.caption("⚠️ Esto es una simulación de usuario, no un login real. Falta agregar autenticación con contraseña antes de usarlo en producción con varios digitadores.")

st.sidebar.markdown("---")
st.sidebar.subheader("⛽ Parámetros Económicos")
precio_diesel_semana = st.sidebar.number_input("Precio Diésel por Galón ($)", min_value=1.0, value=4.50, step=0.10)

st.markdown("<h1 style='color: #007A33;'>🚚 Ransa · Sistema Integral de Gestión Logística (TMS)</h1>", unsafe_allow_html=True)
st.markdown(f"### Operando Cliente: <span style='color:#007A33;'>{cliente_activo}</span>", unsafe_allow_html=True)
st.markdown("---")

tab1, tab2, tab3 = st.tabs(["📋 Despacho (Salidas)", "💰 Recepción (Liquidaciones)", "📊 Reportes e Impacto Diésel"])

# ==========================================
# MÓDULO 1: DESPACHO / CREACIÓN DE VIAJES
# ==========================================
with tab1:
    if perfil_activo in ["Administrador", "Operador"]:
        st.header("Generación de Hoja de Control de Viaje")

        run = st.session_state.form_run  # sufijo de las keys del formulario actual

        c1, c2, c3 = st.columns(3)
        c1.metric("No. Viaje (vista previa)", peek_siguiente_correlativo(cliente_activo))
        c2.text_input("Fecha de Creación", value=datetime.now().strftime("%Y-%m-%d"), disabled=True, key=f"f_crea_{run}")
        c3.text_input("Hora de Creación", value=datetime.now().strftime("%H:%M:%S"), disabled=True, key=f"h_crea_{run}")
        st.caption("El No. de Viaje definitivo se asigna al guardar, para evitar que dos digitadores reciban el mismo número.")

        st.subheader("1. Selección de Transporte")
        col_p, col_t, col_cap, col_pil, col_aux = st.columns(5)

        with col_p:
            placa = st.selectbox("Placa del Camión", [""] + list(st.session_state.catalogos["camiones"].keys()), key=f"placa_{run}")

        t_pred, cap_pred, pil_pred, aux_pred = "", "", "", ""
        if placa:
            datos_c = st.session_state.catalogos["camiones"][placa]
            t_pred = datos_c["transportista"]
            cap_pred = datos_c["tipo"]
            pil_pred = datos_c["piloto"]
            aux_pred = datos_c["auxiliar"]

        with col_t:
            st.text_input("Transportista", value=t_pred, disabled=True, key=f"transp_{run}")
        with col_cap:
            st.text_input("Capacidad Camión", value=cap_pred, disabled=True, key=f"cap_{run}")
        with col_pil:
            pilotos = st.session_state.catalogos["pilotos"]
            piloto_final = st.selectbox("Piloto", pilotos, index=pilotos.index(pil_pred) if pil_pred in pilotos else 0, key=f"piloto_{run}")
        with col_aux:
            auxiliares = st.session_state.catalogos["auxiliares"]
            auxiliar_final = st.selectbox("Auxiliar de Carga", auxiliares, index=auxiliares.index(aux_pred) if aux_pred in auxiliares else 0, key=f"aux_{run}")

        st.subheader("2. Destinos y Control de Marchamos")

        if st.button("➕ Añadir Destino Adicional"):
            st.session_state.num_destinos += 1
            st.rerun()

        tiendas_cliente = st.session_state.catalogos["clientes"][cliente_activo]
        destinos_viaje = []
        total_destinos = st.session_state.num_destinos
        tiendas_usadas_en_form = set()

        for i in range(total_destinos):
            st.markdown(f"📍 **Destino #{i + 1}**")
            d1, d2, d3, d4 = st.columns(4)
            d5, d6, d7 = st.columns(3)

            with d1:
                tienda = d1.selectbox("Tienda / Destino", [""] + list(tiendas_cliente.keys()), key=f"t_{run}_{i}")
                km_t = tiendas_cliente[tienda]["km"] if tienda else 0.0
                gal_t = tiendas_cliente[tienda]["galones_base"] if tienda else 0.0
                st.caption(f"Distancia: {km_t} KM | Diésel: {gal_t} Gal")
            with d2:
                m_ida_tienda = d2.text_input("🔒 Marchamo IDA (Único)", key=f"mida_{run}_{i}")
            with d3:
                es_ultimo = (i == total_destinos - 1)
                m_reg_tienda = d3.text_input("🔄 Marchamo REGRESO", key=f"mreg_{run}_{i}", disabled=not es_ultimo,
                                              placeholder="Solo última tienda" if not es_ultimo else "")
            with d4:
                peds = d4.text_input("No. Pedidos (Separados por coma)", key=f"p_{run}_{i}", placeholder="Ej: P01, P02")

            with d5:
                roles = d5.number_input("Roles Metálicos", min_value=0, step=1, key=f"r_{run}_{i}")
            with d6:
                tarimas = d6.number_input("Tarimas Madera", min_value=0, step=1, key=f"tar_{run}_{i}")
            with d7:
                cajas = d7.number_input("Cajas Manual / WMS", min_value=0, step=1, key=f"c_{run}_{i}")

            if tienda:
                if tienda in tiendas_usadas_en_form:
                    st.warning(f"⚠️ La tienda '{tienda}' ya está agregada como otro destino de este mismo viaje.")
                tiendas_usadas_en_form.add(tienda)
                destinos_viaje.append({
                    "tienda": tienda,
                    "km": km_t,
                    "galones_base": gal_t,
                    "pedidos": peds,
                    "marchamo_ida": m_ida_tienda.strip(),
                    "marchamo_regreso": m_reg_tienda.strip() if es_ultimo else "",
                    "roles": roles,
                    "tarimas": tarimas,
                    "cajas": cajas
                })
            st.markdown("---")

        if st.button("💾 Guardar Viaje y Generar Hoja de Control"):
            marchamos_vacios = any(not d["marchamo_ida"] for d in destinos_viaje)
            marchamos_repetidos_en_form = len([d["marchamo_ida"] for d in destinos_viaje]) != len(
                set(d["marchamo_ida"] for d in destinos_viaje)
            )

            if not placa or len(destinos_viaje) == 0:
                st.error("❌ Error: Debe seleccionar el camión y al menos un destino.")
            elif marchamos_vacios:
                st.error("❌ Error: Todos los destinos ingresados deben tener un Marchamo de Ida asignado.")
            elif marchamos_repetidos_en_form:
                st.error("❌ Error: Hay marchamos de ida repetidos dentro de este mismo viaje.")
            elif total_destinos > 0 and not destinos_viaje[-1]["marchamo_regreso"]:
                st.error("❌ Error: El Marchamo de Regreso es obligatorio en la última tienda para cerrar el circuito.")
            else:
                ok, resultado = guardar_viaje(
                    cliente=cliente_activo,
                    placa=placa,
                    transportista=t_pred,
                    piloto=piloto_final,
                    auxiliar=auxiliar_final,
                    usuario=usuario_activo,
                    destinos_viaje=destinos_viaje
                )
                if ok:
                    st.success(f"✅ Viaje {resultado} guardado correctamente.")
                    st.session_state.num_destinos = 1
                    st.session_state.form_run += 1  # limpia el formulario para el próximo viaje
                    st.rerun()
                else:
                    st.error(f"❌ Error: {resultado}")

        st.markdown("### 🕒 Últimos viajes registrados")
        st.dataframe(obtener_viajes_recientes(), use_container_width=True)
    else:
        st.info("Tu perfil no tiene permisos para despachar viajes.")

# ==========================================
# MÓDULO 2: LIQUIDACIONES
# ==========================================
with tab2:
    if perfil_activo in ["Administrador", "Liquidador"]:
        st.header("Liquidación de Viajes")
        st.caption("Registra lo que el camión trajo de regreso de cada tienda. Las cajas no se devuelven.")

        col_crit, col_val, col_btn = st.columns([1, 2, 1])
        with col_crit:
            criterio = st.selectbox("Buscar por", ["No. de Viaje", "Marchamo de Ida", "Placa"], key="criterio_liq")
        with col_val:
            valor_busqueda = st.text_input("Valor a buscar", key="valor_liq")
        with col_btn:
            st.write("")
            st.write("")
            buscar = st.button("🔍 Buscar Viaje")

        if buscar:
            if valor_busqueda.strip():
                viaje, destinos = buscar_viaje_para_liquidar(valor_busqueda)
                st.session_state["viaje_liq"] = viaje
                st.session_state["destinos_liq"] = destinos
            else:
                st.warning("Escribe un valor para buscar.")

        viaje = st.session_state.get("viaje_liq")
        destinos = st.session_state.get("destinos_liq", [])

        if "viaje_liq" in st.session_state and viaje is None:
            st.warning("No se encontró ningún viaje con ese dato.")

        if viaje:
            st.markdown("---")
            st.subheader(f"Viaje {viaje['id_viaje']}")
            i1, i2, i3, i4 = st.columns(4)
            i1.metric("Cliente", viaje["cliente"])
            i2.metric("Placa", viaje["placa"])
            i3.metric("Piloto", viaje["piloto"])
            i4.metric("Estado", viaje["estado"])

            if viaje["estado"] == "Liquidado":
                st.success(f"✅ Este viaje ya fue liquidado el {viaje['fecha_liquidacion']} "
                            f"{viaje['hora_liquidacion']} por {viaje['usuario_liquido']}.")
            else:
                st.markdown("#### Devoluciones por tienda")
                destinos_actualizados = []
                for d in destinos:
                    st.markdown(f"📍 **{d['tienda']}** — marchamo ida: `{d['marchamo_ida']}`")
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        roles_dev = st.number_input(
                            "Roles devueltos", min_value=0, step=1, key=f"roldev_{d['id']}"
                        )
                    with c2:
                        tarimas_dev = st.number_input(
                            "Tarimas devueltas", min_value=0, step=1, key=f"tardev_{d['id']}"
                        )
                    with c3:
                        pacas_dev = st.number_input(
                            "Pacas de cartón devueltas", min_value=0, step=1, key=f"pacdev_{d['id']}"
                        )
                    destinos_actualizados.append({
                        "id": d["id"],
                        "roles_devueltos": roles_dev,
                        "tarimas_devueltas": tarimas_dev,
                        "pacas_carton_devueltas": pacas_dev
                    })
                    st.markdown("---")

                if st.button("✅ Liquidar Viaje"):
                    ok, msg = liquidar_viaje(viaje["id"], destinos_actualizados, usuario_activo)
                    if ok:
                        st.success(f"Viaje {viaje['id_viaje']} liquidado correctamente. "
                                   "Ya puede procesarse el pago al transportista.")
                        del st.session_state["viaje_liq"]
                        del st.session_state["destinos_liq"]
                        st.rerun()
                    else:
                        st.error(f"❌ Error al liquidar: {msg}")
    else:
        st.info("Tu perfil no tiene permisos para liquidar viajes.")

# ==========================================
# MÓDULO 3: REPORTES (pendiente de construir)
# ==========================================
with tab3:
    st.info("Módulo de reportes en construcción: impacto de diésel por viaje/tienda usando el "
            "precio configurado en la barra lateral.")
