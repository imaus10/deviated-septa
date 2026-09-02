provider "aws" {
  region = var.region
}

locals {
  bucket_id = "deviated-septa-${var.environment}"
  tags = {
    Project     = "deviated-septa"
    Environment = var.environment
  }
}

# ---- Managed CloudFront policies (referenced by AWS names, no hardcoded GUIDs) ----
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_response_headers_policy" "cors_with_preflight" {
  name = "Managed-CORS-With-Preflight"
}

# ---- Private bucket ----
resource "aws_s3_bucket" "this" {
  bucket = local.bucket_id
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  ignore_public_acls      = true
  # Relaxed only in direct-S3 mode (use_cloudfront=false) so the public/*
  # bucket policy below is allowed; fully blocked once CloudFront serves it.
  block_public_policy     = var.use_cloudfront
  restrict_public_buckets = var.use_cloudfront
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# ---- CloudFront OAC + distribution (public/ prefix only; archives stay private) ----
resource "aws_cloudfront_origin_access_control" "this" {
  count                             = var.use_cloudfront ? 1 : 0
  name                              = "deviated-septa-${var.environment}-oac"
  description                       = "OAC for deviated-septa ${var.environment} bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "this" {
  count           = var.use_cloudfront ? 1 : 0
  comment         = "Deviated SEPTA ${var.environment} static hosting"
  enabled         = true
  is_ipv6_enabled = true
  http_version    = "http2and3"
  price_class     = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.this.bucket_regional_domain_name
    origin_id                = "s3-${local.bucket_id}"
    origin_path              = "/public"
    origin_access_control_id = aws_cloudfront_origin_access_control.this[0].id
  }

  default_cache_behavior {
    target_origin_id           = "s3-${local.bucket_id}"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.cors_with_preflight.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
      locations        = []
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = local.tags
}

# ---- Bucket policy: OAC may only GET the public/ prefix ----
resource "aws_s3_bucket_policy" "cloudfront_public_only" {
  count  = var.use_cloudfront ? 1 : 0
  bucket = aws_s3_bucket.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.this.arn}/public/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.this[0].arn
          }
        }
      }
    ]
  })
}

# ---- Direct-from-S3 mode (use_cloudfront=false): world-readable public/ prefix ----
resource "aws_s3_bucket_policy" "public_only" {
  count  = var.use_cloudfront ? 0 : 1
  bucket = aws_s3_bucket.this.id
  # S3 rejects a public policy while BlockPublicPolicy is true — relax it first.
  depends_on = [aws_s3_bucket_public_access_block.this]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.this.arn}/public/*"
      }
    ]
  })
}

resource "aws_s3_bucket_cors_configuration" "this" {
  count  = var.use_cloudfront ? 0 : 1
  bucket = aws_s3_bucket.this.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    max_age_seconds = 300
  }
}

# ---- Poller IAM user, scoped to its own bucket only ----
resource "aws_iam_user" "septa_poller" {
  name = "septa-poller-${var.environment}"
  tags = local.tags
}

resource "aws_iam_user_policy" "septa_poller" {
  name   = "deviated-septa-${var.environment}-bucket"
  user   = aws_iam_user.septa_poller.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.this.arn,
          "${aws_s3_bucket.this.arn}/*",
        ]
      }
    ]
  })
}

resource "aws_iam_access_key" "septa_poller" {
  user = aws_iam_user.septa_poller.name
}