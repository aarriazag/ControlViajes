#!/bin/bash
# Este script corre automáticamente cada vez que Render inicia la app.
# Toma los valores que pusiste en el panel de Render (Environment) y
# arma el archivo de conexión que Streamlit necesita (secrets.toml),
# para no tener que subir esos datos a GitHub en texto plano.
set -e

mkdir -p .streamlit
cat > .streamlit/secrets.toml <<EOF
[postgres]
host = "${SUPABASE_HOST}"
port = ${SUPABASE_PORT}
dbname = "${SUPABASE_DB}"
user = "${SUPABASE_USER}"
password = "${SUPABASE_PASSWORD}"
EOF

streamlit run app.py --server.port="${PORT}" --server.address=0.0.0.0
