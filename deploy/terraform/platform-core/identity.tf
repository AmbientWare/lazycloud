data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  arn_prefix = "arn:${data.aws_partition.current.partition}"
}
