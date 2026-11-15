FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# ffmpeg statique avec NVENC : il charge libnvidia-encode fournie par le runtime NVIDIA du Docker hote
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz
RUN curl -fsSL "$FFMPEG_URL" -o /tmp/ff.tar.xz \
    && tar -xJf /tmp/ff.tar.xz -C /usr/local/bin --strip-components=2 --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' \
    && rm /tmp/ff.tar.xz \
    && ffmpeg -hide_banner -encoders | grep -q hevc_nvenc
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility PYTHONUNBUFFERED=1
VOLUME /data
ENTRYPOINT ["/entrypoint.sh"]
