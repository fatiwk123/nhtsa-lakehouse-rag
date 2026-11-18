# One VM running the docker-compose stack: the same setup as local, no managed service.
# Secrets (Postgres, MinIO) are generated on the VM at first boot: they are neither in
# the Terraform state nor in the instance user data.

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720108477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "this" {
  key_name   = var.project
  public_key = var.ssh_public_key
}

resource "aws_security_group" "this" {
  name        = var.project
  description = "SSH (and optionally the UI) from allowed CIDRs only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  dynamic "ingress" {
    for_each = var.expose_ui ? [8501] : []
    content {
      description = "Streamlit UI"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = var.allowed_cidrs
    }
  }

  egress {
    description = "NHTSA API, Docker Hub, Hugging Face, Ollama registry"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.this.key_name
  vpc_security_group_ids = [aws_security_group.this.id]

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    repo_url          = var.repo_url
    repo_ref          = var.repo_ref
    nhtsa_makes       = var.nhtsa_makes
    nhtsa_model_years = var.nhtsa_model_years
    ollama_model      = var.ollama_model
  })
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  tags = {
    Name = var.project
  }
}
