FROM python:3.11-slim

# git/openssh-client: used by the Git module (GitPython shells out to git)
# and by SSH-based git remotes. docker.io: gives us the `docker` CLI for
# swarm stack deploys (docker-py has no stack support). curl: fetches kubectl.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        docker.io \
        curl \
    && curl -fsSLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python", "run.py"]
