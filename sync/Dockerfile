FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py inject.js status.html ./
ENV STATE_FILE=/state/state.json
EXPOSE 8086
CMD ["python", "app.py", "serve"]
