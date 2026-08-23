#!/usr/bin/env bash
set -euo pipefail

echo "=== Building Docker image ==="
docker build -t resolv:latest .

echo "=== Creating kind cluster ==="
kind create cluster --name resolv --config k8s/kind-config.yaml 2>/dev/null || true

echo "=== Loading image into kind ==="
kind load docker-image resolv:latest --name resolv

echo "=== Applying core manifests ==="
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/redis/
kubectl apply -f k8s/qdrant/
kubectl apply -f k8s/webhook/
kubectl apply -f k8s/workers/

echo "=== Waiting for pods ==="
kubectl -n resolv wait --for=condition=Ready pod --all --timeout=180s

echo "=== All pods ==="
kubectl -n resolv get pods

echo ""
echo "=== Done! ==="
echo "Webhook available at: http://localhost:30080/webhook"
echo "NOTE: Monitoring stack (Prometheus/Grafana/OTel) manifests are in k8s/monitoring/ but not deployed."
echo "      To deploy them on a machine with more RAM: kubectl apply -f k8s/monitoring/"
