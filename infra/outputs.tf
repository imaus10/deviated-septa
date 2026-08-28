output "cloudfront_domain_name" {
  description = "Public URL origin (e.g. https://d123.cloudfront.net)"
  value       = aws_cloudfront_distribution.this.domain_name
}

output "distribution_id" {
  value = aws_cloudfront_distribution.this.id
}

output "bucket_name" {
  value = aws_s3_bucket.this.id
}

output "septa_poller_user" {
  value = aws_iam_user.septa_poller.name
}

output "septa_poller_access_key_id" {
  value = aws_iam_access_key.septa_poller.id
}

output "septa_poller_secret_access_key" {
  description = "Capture this now; it is shown only once at apply time."
  value       = aws_iam_access_key.septa_poller.secret
  sensitive   = true
}