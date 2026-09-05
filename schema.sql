-- ============================================================
-- Ransa · Control de Viajes — Creación de tablas en Supabase
--
-- CÓMO USAR ESTE ARCHIVO:
-- 1. Entra a tu proyecto en supabase.com
-- 2. Click en "SQL Editor" (menú izquierdo) → "New query"
-- 3. Pega TODO este archivo
-- 4. Click en "Run" (o Ctrl+Enter)
-- Es seguro correrlo varias veces: no borra ni duplica datos si
-- las tablas ya existen.
-- ============================================================

CREATE TABLE IF NOT EXISTS contadores (
    cliente TEXT PRIMARY KEY,
    ultimo_correlativo INTEGER NOT NULL DEFAULT 0
);

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
);

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
);

-- Verificación rápida: si esto muestra 3 filas, las tablas se crearon bien.
SELECT tablename FROM pg_tables
WHERE schemaname = 'public'
AND tablename IN ('contadores', 'viajes', 'destinos');
