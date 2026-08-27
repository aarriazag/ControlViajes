import streamlit as st
import pandas as pd
from datetime import datetime

# Configuración de la página web con estilo e identidad corporativa
st.set_page_config(page_title="Ransa - Gestión Logística de Viajes", layout="wide", page_icon="🚚")

# --- ESTILOS VISUALES RANSA (VERDE Y BLANCO) ---
st.markdown("""
    <style>
        /* Color del botón principal */
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
        /* Color de las pestañas activas */
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
# 1. BASE DE DATOS INICIAL (CATÁLOGOS MAESTROS)
# ==========================================
if 'db' not in st.session_state:
    st.session_state.db = {
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

if 'viajes' not in st.session_state:
    st.session_state.viajes = []

if 'marchamos' not in st.session_state:
    st.session_state.marchamos = set()

# ==========================================
# 2. CONTROL DE ACCESO Y CONFIGURACIÓN (SIDEBAR)
# ==========================================
st.sidebar.markdown("<h2 style='color:#007A33; font-weight:bold;'>RANSA</h2>", unsafe_allow_html=True)
st.sidebar.title("🔐 Configuración Inicial")

# Selección fija del Cliente (Se queda guardado para digitación masiva)
cliente_activo = st.sidebar.selectbox("🎯 CLIENTE ACTIVO", list(st.session_state.db["clientes"].keys()))

st.sidebar.markdown("---")
usuario_activo = st.sidebar.selectbox("Simular Usuario Activo", list(st.session_state.db["usuarios"].keys()))
perfil_activo = st.session_state.db["usuarios"][usuario_activo]
st.sidebar.info(f"**Perfil:** {perfil_activo}")

st.sidebar.markdown("---")
st.sidebar.subheader("⛽ Parámetros Económicos")
precio_diesel_semana = st.sidebar.number_input("Precio Diésel por Galón ($)", min_value=1.0, value=4.50, step=0.10)

# Encabezado Principal Corporativo
st.markdown("<h1 style='color: #007A33;'>🚚 Ransa · Sistema Integral de Gestión Logística (TMS)</h1>", unsafe_allow_html=True)
st.markdown(f"### Operando Cliente: <span style='color:#007A33;'>{cliente_activo}</span>", unsafe_allow_html=True)
st.markdown("---")

# Creación de pestañas
tab1, tab2, tab3 = st.tabs(["📋 Despacho (Salidas)", "💰 Recepción (Liquidaciones)", "📊 Reportes e Impacto Diésel"])

# ==========================================
# MÓDULO 1: DESPACHO / CREACIÓN DE VIAJES
# ==========================================
with tab1:
    if perfil_activo in ["Administrador", "Operador"]:
        st.header("Generación de Hoja de Control de Viaje")
        
        id_viaje = len(st.session_state.viajes) + 1
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        hora_hoy = datetime.now().strftime("%H:%M:%S")
        
        c1, c2, c3 = st.columns(3)
        c1.metric("No. Viaje Correlativo", f"#{id_viaje}")
        c2.text_input("Fecha de Creación", value=fecha_hoy, disabled=True, key="f_crea")
        c3.text_input("Hora de Creación", value=hora_hoy, disabled=True, key="h_crea")
        
        st.subheader("1. Selección de Transporte")
        col_p, col_t, col_cap, col_pil, col_aux = st.columns(5)
        
        with col_p:
            placa = st.selectbox("Placa del Camión", [""] + list(st.session_state.db["camiones"].keys()))
            
        t_pred, cap_pred, pil_pred, aux_pred = "", "", "", ""
        if placa:
            datos_c = st.session_state.db["camiones"][placa]
            t_pred = datos_c["transportista"]
            cap_pred = datos_c["tipo"]
            pil_pred = datos_c["piloto"]
            aux_pred = datos_c["auxiliar"]
            
        with col_t: st.text_input("Transportista", value=t_pred, disabled=True)
        with col_cap: st.text_input("Capacidad Camión", value=cap_pred, disabled=True)
        with col_pil: piloto_final = st.selectbox("Piloto", st.session_state.db["pilotos"], index=st.session_state.db["pilotos"].index(pil_pred) if pil_pred in st.session_state.db["pilotos"] else 0)
        with col_aux: auxiliar_final = st.selectbox("Auxiliar de Carga", st.session_state.db["auxiliares"], index=st.session_state.db["auxiliares"].index(aux_pred) if aux_pred in st.session_state.db["auxiliares"] else 0)
        
        st.subheader("2. Destinos y Control de Marchamos")
        
        if f'num_dest_{id_viaje}' not in st.session_state:
            st.session_state[f'num_dest_{id_viaje}'] = 1
            
        if st.button("➕ Añadir Destino Adicional"):
            st.session_state[f'num_dest_{id_viaje}'] += 1
            st.rerun()
            
        tiendas_cliente = st.session_state.db["clientes"][cliente_activo]
        destinos_viaje = []
        total_destinos = st.session_state[f'num_dest_{id_viaje}']
        
        # Iteración optimizada para el digitador
        for i in range(total_destinos):
            st.markdown(f"📍 **Destino #{i+1}**")
            d1, d2, d3, d4 = st.columns(4)
            d5, d6, d7 = st.columns(3)
            
            with d1:
                tienda = d1.selectbox("Tienda / Destino", [""] + list(tiendas_cliente.keys()), key=f"t_{id_viaje}_{i}")
                km_t = tiendas_cliente[tienda]["km"] if tienda else 0.0
                gal_t = tiendas_cliente[tienda]["galones_base"] if tienda else 0.0
                st.caption(f"Distancia: {km_t} KM | Diésel: {gal_t} Gal")
            with d2: 
                m_ida_tienda = d2.text_input("🔒 Marchamo IDA (Único)", key=f"mida_{id_viaje}_{i}")
            with d3:
                es_ultimo = (i == total_destinos - 1)
                m_reg_tienda = d3.text_input("🔄 Marchamo REGRESO", key=f"mreg_{id_viaje}_{i}", disabled=not es_ultimo, placeholder="Solo última tienda" if not es_ultimo else "")
            with d4: 
                peds = d4.text_input("No. Pedidos (Separados por coma)", key=f"p_{id_viaje}_{i}", placeholder="Ej: P01, P02")
                
            with d5: roles = d5.number_input("Roles Metálicos", min_value=0, step=1, key=f"r_{id_viaje}_{i}")
            with d6: tarimas = d6.number_input("Tarimas Madera", min_value=0, step=1, key=f"tar_{id_viaje}_{i}")
            with d7: cajas = d7.number_input("Cajas Manual / WMS", min_value=0, step=1, key=f"c_{id_viaje}_{i}")
            
            if tienda:
                destinos_viaje.append({
                    "tienda": tienda, 
                    "km": km_t, 
                    "galones_base": gal_t, 
                    "pedidos": peds,
                    "marchamo_ida": m_ida_tienda, 
                    "marchamo_regreso": m_reg_tienda if es_ultimo else "N/A",
                    "roles": roles, 
                    "tarimas": tarimas, 
                    "cajas": cajas
                })
            st.markdown("---")
            
        if st.button("💾 Guardar Viaje y Generar Hoja de Control"):
            errores_marchamo = False
            marchamos_vacios = False
            
            for dest in destinos_viaje:
                if not dest["marchamo_ida"]:
                    marchamos_vacios = True
                if dest["marchamo_ida"] in st.session_state.marchamos:
                    errores_marchamo = True
                    
            if not placa or len(destinos_viaje) == 0:
                st.error("❌ Error: Debe seleccionar el camión y al menos un destino.")
            elif marchamos_vacios:
                st.error("❌ Error: Todos los destinos ingresados deben tener un Marchamo de Ida asignado.")
            elif errores_marchamo:
                st.error("❌ Error: Uno o más Marchamos de Ida digitados ya fueron utilizados en viajes anteriores.")
            elif total_destinos > 0 and not destinos_viaje[-1]["marchamo_regreso"]:
                st.error("❌ Error: El Marchamo de Regreso es obligatorio en la última tienda para cerrar el circuito.")
            else:
                # Modificado a estructura limpia y directa sin saltos de línea conflictivos
                nuevo_viaje = {}
                nuevo_viaje["id_viaje"] = id_viaje
                nuevo_viaje["usuario_creador"] = usuario_activo
                nuevo_viaje["fecha_creacion"] = fecha_hoy
