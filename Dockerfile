FROM python:3.11-slim

WORKDIR /app

# Dependente sistem
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instaleaza dependentele Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiaza codul
COPY . .

# Creeaza directoarele necesare
RUN mkdir -p data logs

# Ruleaza ca non-root
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "apex_scalper"]
