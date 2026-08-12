FROM python:3.12-slim
COPY app.py collector.py janitor.py dash.html /
EXPOSE 8080
CMD ["python","/app.py"]
