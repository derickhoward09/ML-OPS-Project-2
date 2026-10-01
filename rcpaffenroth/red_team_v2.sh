#!/bin/bash

# Number possible groups
num_files=25

# vars
PORT=22000
MACHINE=paffenroth-23.dyn.wpi.edu
KEY="$HOME/.ssh/mlops/student-admin_key"

# Loop to create files
for i in $(seq 2 "$num_files"); do
  echo "trying group ${i} at port $((${i} + ${PORT})) "
  echo "------------------------------------------------"
  echo "------------------------------------------------"
  if ssh -J akrett@turing.wpi.edu -i "$KEY" -p $((${i} + ${PORT})) -o StrictHostKeyChecking=no student-admin@${MACHINE} hostname; then
    echo "group ${i} is vulnerable!"
  else
    echo "group ${i} is protected!"
  fi
  echo "------------------------------------------------"
  echo "------------------------------------------------"
done
