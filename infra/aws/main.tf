terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "name" {
  type    = string
  default = "referral-events"
}

variable "max_receive_count" {
  type    = number
  default = 4 # matches MAX_ATTEMPTS in src/cwl/bus.py
}

resource "aws_kms_key" "events" {
  description         = "${var.name} queue encryption"
  enable_key_rotation = true
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-dlq"
  kms_master_key_id         = aws_kms_key.events.arn
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "main" {
  name                       = var.name
  kms_master_key_id          = aws_kms_key.events.arn
  visibility_timeout_seconds = 60
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

data "aws_iam_policy_document" "tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.main.arn, aws_sqs_queue.dlq.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  policy    = data.aws_iam_policy_document.tls_only.json
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = data.aws_iam_policy_document.tls_only.json
}

data "aws_iam_policy_document" "producer" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.events.arn]
  }
}

data "aws_iam_policy_document" "consumer" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.main.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.events.arn]
  }
}

resource "aws_iam_policy" "producer" {
  name   = "${var.name}-producer"
  policy = data.aws_iam_policy_document.producer.json
}

resource "aws_iam_policy" "consumer" {
  name   = "${var.name}-consumer"
  policy = data.aws_iam_policy_document.consumer.json
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name}-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

output "queue_url" { value = aws_sqs_queue.main.id }
output "dlq_url" { value = aws_sqs_queue.dlq.id }
output "producer_policy_arn" { value = aws_iam_policy.producer.arn }
output "consumer_policy_arn" { value = aws_iam_policy.consumer.arn }
