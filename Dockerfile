FROM python:3.12-slim
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /home/user/app
ADD --chown=user https://github.com/jacobhroutzong/harness2-lab/archive/99e357702747695c5b9a5047109e07acd8c3b312.tar.gz /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 -C /home/user/app && pip install --no-cache-dir --user -r requirements.txt
EXPOSE 7860
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "7860", "--proxy-headers"]

