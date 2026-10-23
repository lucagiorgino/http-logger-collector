# Back-end project assignment

## Introduction
"Amazing HTTP Logger" is an application designed to intercept and log HTTP traffic generated on user devices in order to perform threat detection and analysis.

The application is structured with the following components:
- Software agents, installed on end-user devices, whose purpose is to locally intercept and log HTTP traffic on behalf of the user
- Backend services, whose purpose is to perform end-user authentication and to collect HTTP traffic logs sent by agents

In the context of the "Amazing HTTP Logger" application, you are asked to design a set of APIs in order to implement the backend services as defined above. More in particular, the APIs you design and implement will be entitled to:
- authenticate the end-users on behalf of which the agents communicate with the backend services
- store the HTTP traffic logs received by the agents.


## Requirements

### MUST-HAVE
In order to accomplish the task, you are asked to:
- design the database structure which will store the data (*MongoDB* database)
- design and implement the set of APIs to perform authentication and authorization: even though you can implement the whole authentication and authorization process from scratch, it is strongly suggested to use a third-party identity provider (e.g. Auth0, Okta, ...)
- design and implement the set of APIs to receive the HTTP traffic logs sent by the agents, and store them in the database

The project must be **containerized** and it must be compliant with the cloud-native development principles.

All the code must be designed and implemented with **security in mind** (strong requirement).


### NICE-TO-HAVE
- Linter configuration
- Unit-tests configuration
- Unit-tests implementation for all the functionalities implemented (it is strongly suggested to adopt the TDD methodology)
- Container orchestration (e.g. k8s, docker swarm, docker-compose)
- Implementing the backend services with a serverless flavour

## Sample APIs
As sample, here below you can find a set of APIs that may be provided by the backend services. You can implement such APIs, or decide to implement a set of completely different ones, according with your needs.

### Authentication and authorization APIs
They include all the APIs involved in the authentication and authorization process.

```
- /auth/authorize
- /auth/token
```

### Agent APIs
They include all the APIs used by the agents to send the HTTP traffic logs (URL, headers, body, etc...)
```
- /agent/traffic_logs
```

## Technologies
The project can be implemented using either Python or Javascript/Typescript (NodeJs) as programming language, and you can pick whatever framework you want as well, as long as it respects the requirements.

It is required to containerize the application using Docker, but you can choose which orchestrator to use if you decide to implement container orchestration.


## Submission
The submission can be done by sending the bundle file generated through `git bundle create <bundle_name> master`. To verify the correctness of the bundle, it can be unbundled using `git clone -b master <bundle_name> <destination_folder>`.


**IMPORTANT**:
Keep in mind that, during the evaluation, `git` usage (logs history, commit messages, etc...) and non-functional requirements (scalability, performance) will be taken into account as well.


## Useful links
- [Auth0 integration](https://auth0.com/docs/)
- [Okta integration](https://developer.okta.com/)
- [The Twelve-Factor App](https://12factor.net/)
- [Cloud-native principles by Microsoft](https://docs.microsoft.com/en-us/dotnet/architecture/modernize-with-azure-containers/modernize-existing-apps-to-cloud-optimized/what-about-cloud-native-applications)
- [Cloud-native principles by Google](https://cloud.google.com/blog/products/application-development/5-principles-for-cloud-native-architecture-what-it-is-and-how-to-master-it)
- [Cloud-native principles by IBM](https://www.ibm.com/cloud/architecture/architecture/practices/cloud-native-principles)
