package main

import (
	"flag"
	"os"

	"go.uber.org/zap/zapcore"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	controlv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
	agentruncontroller "github.com/tiramitree/benchhandoff/controller/internal/controller"
)

func main() {
	var developmentLogs bool
	flag.BoolVar(
		&developmentLogs,
		"development-logs",
		false,
		"use human-readable logs without adding workload output",
	)
	flag.Parse()

	logOptions := zap.Options{
		Development: developmentLogs,
		TimeEncoder: zapcore.ISO8601TimeEncoder,
	}
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&logOptions)))
	log := ctrl.Log.WithName("setup")

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		log.Error(err, "unable to add the Kubernetes scheme")
		os.Exit(1)
	}
	if err := controlv1alpha1.AddToScheme(scheme); err != nil {
		log.Error(err, "unable to add the AgentRun scheme")
		os.Exit(1)
	}

	manager, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: "0",
		LeaderElection:         false,
	})
	if err != nil {
		log.Error(err, "unable to create manager")
		os.Exit(1)
	}

	reconciler := &agentruncontroller.AgentRunReconciler{
		Client: manager.GetClient(),
		Scheme: manager.GetScheme(),
	}
	if err := reconciler.SetupWithManager(manager); err != nil {
		log.Error(err, "unable to register AgentRun controller")
		os.Exit(1)
	}

	log.Info("starting AgentRun manager")
	if err := manager.Start(ctrl.SetupSignalHandler()); err != nil {
		log.Error(err, "manager stopped")
		os.Exit(1)
	}
}
