output "public_ip" {
  value = aws_instance.this.public_ip
}

output "ssh" {
  value = "ssh ubuntu@${aws_instance.this.public_ip}"
}

output "wait_for_bootstrap" {
  description = "Blocks until the stack is up (first boot: ~15-20 min, images build + full pipeline run)."
  value       = "ssh ubuntu@${aws_instance.this.public_ip} 'cloud-init status --wait && tail -n 5 /var/log/nhtsa-bootstrap.log'"
}

output "tunnel" {
  description = "UI, API and Dagster on localhost without opening any port."
  value       = "ssh -N -L 8501:localhost:8501 -L 8000:localhost:8000 -L 3000:localhost:3000 ubuntu@${aws_instance.this.public_ip}"
}

output "ui_url" {
  value = var.expose_ui ? "http://${aws_instance.this.public_ip}:8501" : "http://localhost:8501 (through the tunnel)"
}
