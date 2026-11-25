import json

import pulumi
import pulumi_aws as aws

config = pulumi.Config()
aws_config = pulumi.Config("aws")

region = aws_config.require("region")
image_uri = config.get("imageUri")

project = pulumi.get_project()
stack = pulumi.get_stack()
name = f"{project}-{stack}"

# Find the default VPC and its subnets.
vpc = aws.ec2.get_vpc(default=True)
subnets = aws.ec2.get_subnets(filters=[{"name": "vpc-id", "values": [vpc.id]}])

# Create a private ECR repository.
repository = aws.ecr.Repository(
    "application-repository",
    name=f"{name}-webapp",
    image_scanning_configuration={
        "scan_on_push": True,
    },
    image_tag_mutability="MUTABLE",
    force_delete=True,
)

# ECS execution role.
execution_role = aws.iam.Role(
    "ecs-task-execution-role",
    assume_role_policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {
                "Service": "ecs-tasks.amazonaws.com"
            },
            "Action": "sts:AssumeRole",
        }],
    }),
)

execution_policy = aws.iam.RolePolicyAttachment(
    "ecs-task-execution-policy",
    role=execution_role.name,
    policy_arn=(
        "arn:aws:iam::aws:policy/"
        "service-role/AmazonECSTaskExecutionRolePolicy"
    ),
)

# Allow public inbound traffic on application port 8080.
security_group = aws.ec2.SecurityGroup(
    "application-security-group",
    description="Zuno application security group",
    vpc_id=vpc.id,
    ingress=[{
        "protocol": "tcp",
        "from_port": 8080,
        "to_port": 8080,
        "cidr_blocks": ["0.0.0.0/0"],
        "description": "Public application access",
    }],
    egress=[{
        "protocol": "-1",
        "from_port": 0,
        "to_port": 0,
        "cidr_blocks": ["0.0.0.0/0"],
        "description": "Outbound internet access",
    }],
)

# ECS cluster.
cluster = aws.ecs.Cluster(
    "application-cluster",
    name=f"{name}-cluster",
)

# CloudWatch logs.
log_group = aws.cloudwatch.LogGroup(
    "application-log-group",
    name=f"/ecs/{name}",
    retention_in_days=1,
)

# Create the ECS task and service after the image is available.
if image_uri:
    task_definition = aws.ecs.TaskDefinition(
        "application-task-definition",
        family=f"{name}-task",
        requires_compatibilities=["FARGATE"],
        network_mode="awsvpc",
        cpu="256",
        memory="512",
        execution_role_arn=execution_role.arn,
        container_definitions=pulumi.Output.all(
            log_group.name
        ).apply(
            lambda values: json.dumps([{
                "name": "webapp",
                "image": image_uri,
                "essential": True,
                "portMappings": [{
                    "containerPort": 8080,
                    "hostPort": 8080,
                    "protocol": "tcp",
                }],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": values[0],
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "webapp",
                    },
                },
            }])
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[execution_policy, log_group],
        ),
    )

    service = aws.ecs.Service(
        "application-service",
        name=f"{name}-service",
        cluster=cluster.arn,
        task_definition=task_definition.arn,
        desired_count=1,
        launch_type="FARGATE",
        deployment_minimum_healthy_percent=0,
        deployment_maximum_percent=100,
        network_configuration={
            "subnets": subnets.ids,
            "security_groups": [security_group.id],
            "assign_public_ip": True,
        },
        deployment_circuit_breaker={
            "enable": True,
            "rollback": True,
        },
    )

    pulumi.export("serviceName", service.name)
    pulumi.export("clusterName", cluster.name)

pulumi.export("repositoryUrl", repository.repository_url)
pulumi.export("region", region)
