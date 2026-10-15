FROM python:3.11-slim

# Install GCC (C compiler) and OpenJDK (javac/java compiler)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    default-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860
ENV PORT=7860
CMD ["gunicorn", "-b", "0.0.0.0:7860", "--workers", "2", "--timeout", "60", "app:app"]
