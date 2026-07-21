FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# bgutil プロバイダーのURL(docker-composeのサービス名で名前解決される)
ENV BGUTIL_POT_PROVIDER_URL=http://pot-provider:4416

EXPOSE 8000

CMD ["python", "-m", "app.main"]
