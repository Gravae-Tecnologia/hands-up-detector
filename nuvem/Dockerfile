# Imagem enxuta de proposito: com escala-a-zero o cold start entra no caminho
# da primeira deteccao. Sem torch e sem ultralytics a imagem fica em ~400 MB
# e sobe em segundos, em vez dos 3+ GB de uma pilha de treino.
FROM python:3.12-slim

# libgl e libglib: o opencv-headless ainda liga contra elas
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir onnxruntime==1.22.0 numpy opencv-python-headless

COPY motor.py app.py ./
# modelos/ precisa ter o detector nas DUAS orientacoes, gerado por
# `python exportar_detector.py` (yolo11s_416x640 e yolo11s_640x416). Sem o em pe
# a imagem sobe igual, mas quadro de camera "story" usa a entrada deitada e
# aproveita ~37% da largura - pessoa do fundo some. GET / lista `orientacoes`.
COPY modelos/ ./modelos/

ENV PYTHONUNBUFFERED=1 MODELOS=/app/modelos
EXPOSE 8080
CMD ["python", "app.py"]
