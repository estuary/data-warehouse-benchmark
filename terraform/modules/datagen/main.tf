// Datagen EC2 instance — on-demand box for TPC-DS / DSB generation + S3 staging.
//
// Laptop generation caps out well before SF1000 (1 TB): local disk, the
// Docker Desktop VM's ~100 GB ceiling (dsdgen ignores fprintf errors, so a
// full VM disk silently truncates its output), and residential uplink for
// the S3 push. This module provisions a Linux box
// where the harness runs dsdgen natively (no Docker), DuckDB's TPC-DS
// generator gets real cores/RAM, and `benchmark stage-data` pushes to S3
// in-region via an instance role (no long-lived keys on the box).
//
// Opt-in from the root stack via `datagen_enabled` — spin it up for a
// generation campaign, `terraform apply` with it false to tear it down. The
// instance is stateless: everything durable lands in the staging bucket.
//
// Bootstrap (all in user-data):
//   BEFORE apply, upload the repo tarball so the box can self-provision
//   (it has no GitHub credentials; the staging bucket is its delivery channel):
//     git archive --format=tar.gz -o /tmp/benchmark-repo.tar.gz HEAD
//     aws s3 cp /tmp/benchmark-repo.tar.gz \
//       s3://<staging-bucket>/bootstrap/benchmark-repo.tar.gz --profile benchmark
//   User-data then: formats + mounts /data, installs the dsdgen toolchain,
//   AWS CLI, and uv; extracts the tarball to /data/repo; writes the
//   region-only `[profile benchmark]` ~/.aws/config (makes the harness's
//   boto3 session fall through to the instance role — no keys on the box);
//   writes env/benchmark.env + env/aws.env; and runs `uv sync`. If the
//   tarball is absent at boot, everything else still provisions — re-run the
//   repo steps by hand or reboot after uploading. Progress/errors land in
//   /var/log/datagen-bootstrap.log.
//
//   Then generation is immediately runnable, e.g.:
//     cd /data/repo && uv run benchmark generate-tpcds --scales 1000
//
//   NOTE: a user_data change stops and starts a running instance (the AWS
//   provider's default); it does not replace it.

variable "region" {
  description = "AWS region (must match the staging bucket's region for free in-region S3 push)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Generation is CPU + disk bound; m8i.8xlarge = 32 vCPU / 128 GiB."
  type        = string
  default     = "m8i.8xlarge"
}

variable "data_volume_gb" {
  description = "Size of the dedicated gp3 data volume mounted at /data. SF100 needs ~200 GB headroom; SF1000 ~2.5 TB (raw .dat + parquet)."
  type        = number
  default     = 3500
}

variable "data_volume_throughput_mbps" {
  description = "gp3 provisioned throughput for the data volume."
  type        = number
  default     = 1000
}

variable "data_volume_iops" {
  description = "gp3 provisioned IOPS for the data volume."
  type        = number
  default     = 8000
}

variable "ssh_public_key" {
  description = "OpenSSH public key material for the operator key pair. Leave empty to rely on SSM Session Manager only."
  type        = string
  default     = ""
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH (e.g. the VPN egress /32). Ignored when ssh_public_key is empty."
  type        = string
  default     = ""
}

variable "staging_bucket_arn" {
  description = "ARN of the S3 staging bucket the instance role may read/write."
  type        = string
}

variable "staging_bucket_name" {
  description = "Name of the staging bucket — used by user-data to pull the repo tarball (bootstrap/benchmark-repo.tar.gz) and written into the box's env/benchmark.env."
  type        = string
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "datagen" {
  count      = var.ssh_public_key != "" ? 1 : 0
  key_name   = "benchmark-datagen"
  public_key = var.ssh_public_key
}

resource "aws_security_group" "datagen" {
  name        = "benchmark-datagen"
  description = "Datagen box: optional operator SSH in, all egress out."
  vpc_id      = data.aws_vpc.default.id

  dynamic "ingress" {
    for_each = var.ssh_public_key != "" && var.allowed_ssh_cidr != "" ? [1] : []
    content {
      description = "Operator SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [var.allowed_ssh_cidr]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Project = "benchmark-2026" }
}

// Instance role: staging-bucket read/write + SSM Session Manager (keyless
// shell access — works even with no SSH ingress at all).
resource "aws_iam_role" "datagen" {
  name = "benchmark-datagen"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Project = "benchmark-2026" }
}

resource "aws_iam_role_policy" "datagen_s3" {
  name = "staging-bucket-rw"
  role = aws_iam_role.datagen.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = var.staging_bucket_arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ]
        Resource = "${var.staging_bucket_arn}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "datagen_ssm" {
  role       = aws_iam_role.datagen.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "datagen" {
  name = "benchmark-datagen"
  role = aws_iam_role.datagen.name
}

resource "aws_instance" "datagen" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.datagen.id]
  iam_instance_profile   = aws_iam_instance_profile.datagen.name
  key_name               = var.ssh_public_key != "" ? aws_key_pair.datagen[0].key_name : null

  root_block_device {
    volume_size = 50
    volume_type = "gp3"
  }

  # Dedicated data volume — formatted + mounted at /data by user-data.
  ebs_block_device {
    device_name = "/dev/sdf"
    volume_size = var.data_volume_gb
    volume_type = "gp3"
    throughput  = var.data_volume_throughput_mbps
    iops        = var.data_volume_iops
  }

  user_data = <<-EOF
    #!/usr/bin/env bash
    set -euo pipefail
    exec > /var/log/datagen-bootstrap.log 2>&1
    # Data volume: NVMe device name varies; pick the disk with no partitions
    # and no filesystem (the root disk always has partitions, so `lsblk -dn`
    # alone would match it first).
    DEV=""
    for d in $(lsblk -dn -o NAME,TYPE | awk '$2 == "disk" {print $1}'); do
      if [ -z "$(lsblk -n "/dev/$d" | tail -n +2)" ] && ! blkid "/dev/$d" >/dev/null 2>&1; then
        DEV="/dev/$d"; break
      fi
    done
    if [ -n "$DEV" ]; then
      mkfs.ext4 -L datagen "$DEV"
    fi
    mkdir -p /data
    grep -q LABEL=datagen /etc/fstab || echo "LABEL=datagen /data ext4 defaults,nofail 0 2" >> /etc/fstab
    mount -a
    chown ubuntu:ubuntu /data
    # Toolchain: dsdgen native build (make/gcc/flex/bison/yacc), AWS CLI for
    # the S3 repo pull + harness staging, uv for the Python harness.
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y build-essential git flex bison byacc unzip
    snap install aws-cli --classic
    sudo -u ubuntu sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    # Harness runtime config: region-only named profile (boto3 falls through
    # to the instance role for credentials) + the two env files the harness
    # reads. No secrets on the box.
    sudo -u ubuntu bash <<'UBUNTU'
    set -euo pipefail
    mkdir -p "$HOME/.aws"
    printf '[profile benchmark]\nregion=${var.region}\n' > "$HOME/.aws/config"
    UBUNTU
    # Repo: pull the operator-uploaded tarball if present (best-effort — the
    # box still provisions without it; upload + reboot, or extract by hand).
    sudo -u ubuntu bash <<'UBUNTU' || echo "repo bootstrap skipped (tarball missing?)"
    set -euo pipefail
    export PATH="$HOME/.local/bin:$PATH"
    mkdir -p /data/repo && cd /data/repo
    aws s3 cp "s3://${var.staging_bucket_name}/bootstrap/benchmark-repo.tar.gz" repo.tar.gz --region ${var.region} --quiet
    tar xzf repo.tar.gz && rm repo.tar.gz
    mkdir -p env
    printf 'BENCHMARK_S3_BUCKET=${var.staging_bucket_name}\n' > env/benchmark.env
    printf 'AWS_REGION=${var.region}\nAWS_PROFILE=benchmark\n' > env/aws.env
    uv sync
    UBUNTU
    echo "datagen bootstrap complete $(date -u)"
  EOF

  tags = {
    Name    = "benchmark-datagen"
    Project = "benchmark-2026"
  }
}

output "instance_id" {
  value = aws_instance.datagen.id
}

output "public_ip" {
  value = aws_instance.datagen.public_ip
}

output "ssh_command" {
  value = var.ssh_public_key != "" ? "ssh ubuntu@${aws_instance.datagen.public_ip}" : "aws ssm start-session --target ${aws_instance.datagen.id}"
}
