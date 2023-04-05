# Define base image
FROM mambaorg/micromamba

# Copy files into docker image
COPY --chown=$MAMBA_USER:$MAMBA_USER ../../* /tmp/orfrater/

# Run mamba for env.yaml
RUN micromamba install -y -n base -f /tmp/orfrater/env.yaml && \
    micromamba clean --all --yes

ARG MAMBA_DOCKERFILE_ACTIVATE=1

USER root

# Run pip for python dependencies
RUN pip install --no-cache-dir -r /tmp/orfrater/requirements.txt && \
    pip install --no-cache-dir plastid==0.4.8 && \
    rm -Rf /root/.cache/pip

# Set up 
ENV PATH /tmp/orfrater:${PATH}

