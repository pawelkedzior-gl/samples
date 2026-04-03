# Context should be the workspace root (where both sdk/ and samples/ are)

# The Python version must match with the version of pre-fetched dependencies.
# See the temporary python environment in README.md
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy the SDK and server code to preserve relative paths
COPY sdk /app/sdk
COPY samples/rest/python/server /app/samples/rest/python/server
COPY samples/rest/python/test_data /app/samples/rest/python/test_data

# **Pre-prepared dependencies from cloudtop**
# The user must run 'uv export' and 'uv pip install -t python_deps ...' locally first.
COPY samples/rest/python/server/python_deps /app/python_deps

# Set PYTHONPATH so python can find the vendored dependencies and local modules (including the SDK)
ENV PYTHONPATH="/app/python_deps:/app/samples/rest/python/server:/app/sdk/python/src"

WORKDIR /app/samples/rest/python/server

# Initialize database using the vendored dependencies
RUN mkdir -p /app/data && \
    python3 import_csv.py \
    --products_db_path=/app/data/products.db \
    --transactions_db_path=/app/data/transactions.db \
    --data_dir=/app/samples/rest/python/test_data/flower_shop

# Expose port (Cloud Run sets PORT env var)
# We use shell form to expand $PORT
CMD python3 server.py \
    --products_db_path=/app/data/products.db \
    --transactions_db_path=/app/data/transactions.db \
    --port=$PORT \
    --woo_commerce=http://34.118.53.208:8080

