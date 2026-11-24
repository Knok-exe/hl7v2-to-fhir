FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
WORKDIR /data
EXPOSE 2575
ENTRYPOINT ["hl7-fhir"]
CMD ["listen", "--host", "0.0.0.0"]
