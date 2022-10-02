import os, os.path, sys
import collections
import builtins
import math
import argparse
import json
import yaml 
import shutil
import numpy as np 

from itertools import chain
from functools import partial

from bamboo import treefunctions as op
from bamboo.scalefactors import get_correction
from bamboo.analysismodules import NanoAODModule, NanoAODHistoModule, HistogramsModule
from bamboo.analysisutils import makeMultiPrimaryDatasetTriggerSelection
from bamboo.analysisutils import configureJets, configureType1MET, configureRochesterCorrection
from bamboo.root import gbl, addIncludePath, loadHeader

zaPath = os.path.dirname(__file__)
if zaPath not in sys.path: sys.path.append(zaPath)

# Avoid tensorflow print on standard error
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import logging
numba_logger = logging.getLogger('numba')
numba_logger.setLevel(logging.WARNING)

import utils as utils
logger = utils.ZAlogger(__name__)

import corrections as corr
import ControlPLots as cp

from EXtraPLots import * 
from boOstedEvents import get_DeepDoubleX, get_BoostedEventWeight, get_bestSubjetsCut


class NanoHtoZABase(NanoAODModule):
    """ H/A ->Z(ll) H/A(bb) full run2 Ulegacy analysis """
    def __init__(self, args):
        super(NanoHtoZABase, self).__init__(args)
        self.plotDefaults = {
                            "y-axis"           : "Events",
                            "log-y"            : "both",
                            "y-axis-show-zero" : True,
                            "log-y-axis-range" : [10e-4, 10e8],
                            "save-extensions"  : ["pdf", "png"],
                            "show-ratio"       : True,
                            "sort-by-yields"   : False,
                            "legend-columns"   : 3 }
        
        self.doSysts          = self.args.systematic
        self.doEvaluate       = self.args.DNN_Evaluation
        self.doSplitJER       = self.args.splitJER
        self.doJES            = self.args.jes
        self.doHLT            = self.args.hlt
        self.doBlinded        = self.args.blinded
        self.doNanoAODversion = self.args.nanoaodversion
        self.doMETT1Smear     = self.args.doMETT1Smear
        self.dobJetER         = self.args.dobJetEnergyRegression
        self.doYields         = self.args.yields
        self.doSkim           = self.args.skim
        
        self.doPass_bTagEventWeight = True
        self.CleanJets_fromPileup   = False
        self.doDDBvsL               = True
        self.BTV_discrCuts          = False
        self.doDY_reweighting       = True        # using poly fit degree 6 on mjj mass  
        self.doOnlylightflav        = False       # DY reweighting will be applied on light flavour jets only 
        self.dotthDY_reweighting    = False       # tth weights eraly beginning on noSel
        self.doTop_reweighting      = True
        self.doProduceParquet       = False       # df for skim 
        self.doProduceSummedPlots   = False
        self.doSaveQCDVars          = True
        self.SplitSignalPoints      = False       # Distribute generated signal mass points between resolved and boosted
        self.doPassNbr_subjets      = "atleast_1subjet" # discri cut on fat jet options: or both_subjets_pass, fatjet_pass 
        self.WorkingPoints          = ["L", "M", "T"] 
        self.rebin                  = "uniform"   # bayesian or uniform (50 bins) : for the DNN template that will be given to combine
        self.reweightDY             = "split"     # comb or "split" lepton flavour
        self.doCorrect              = "subjets"   # "fatjet" or subjets : when applying btagging SFs 
        self.qcdScaleVarMode        = "separate"  # "separate" : (muR/muF variations)  or combine : (7-point envelope)
        self.pdfVarMode             = "simple"    # simple  : (event-based envelope) (only if systematics enabled)
                                                  # or full : PDF uncertainties (100 histogram variations) 

    def addArgs(self, parser):
        super(NanoHtoZABase, self).addArgs(parser)
        parser.add_argument("-s", "--systematic", action="store_true", help="Produce systematic variations")
        parser.add_argument("-y", "--yields", action="store_true", default= False, help=" add Yields Histograms: not recomended if you turn off the systematics, jobs may run out of memory")
        parser.add_argument("-dnn", "--DNN_Evaluation", action="store_true", help="Pass TensorFlow model and evaluate DNN output")
        parser.add_argument("--splitJER", action="store_true", default= False, help="breakup into 6 nuisance parameters per year (correlated among all jets in all events per year, but uncorrelated across years), useful for analysis that are sensitive to JER, i.e. analyses that are able to constrain the single JER nuisance parameter per year w.r.t. their assigned uncertainty")
        parser.add_argument("--jes", type=str, default="total", choices = ["total", "merged", "full"], help="Run 2 reduced set of JES uncertainty splited by sources or use total")
        parser.add_argument("--hlt", action="store_true", help="Produce HLT efficiencies maps")
        parser.add_argument("--blinded", action="store_true", help="Options to be blind on data if you want to Evaluate the training OR The Ellipses model ")
        parser.add_argument("--nanoaodversion", default="v9", choices= ["v9", "v8", "v7", "v5"], help="version NanoAODv2(== v8 == ULegacy) and NanoAODvv9(== ULeagacy), the rest is pre-Legacy(== EOY) ")
        parser.add_argument("--process", required=False, nargs="+", choices= ["ggH", "bbH"], help="signal process that you wanna to look to ")
        parser.add_argument("--doMETT1Smear", action="store_true", default= False, help="do T1 MET smearing")
        parser.add_argument("--dobJetEnergyRegression", action="store_true", default= False, help="apply b jets energy regression to improve the bjets mass resolution")
        parser.add_argument("--skim", action="store_true", default= False, help="make skim instead of plots")
        parser.add_argument("--backend", type=str, default="dataframe", help="Backend to use, 'dataframe' (default) or 'lazy' or 'compile' for debug mode")
    
    def customizeAnalysisCfg(self, config=None):
        if not self.args.distributed:
            os.system('(git log -n 1;git diff .) &> %s/git.log' % self.args.output)
            with open(os.path.join(self.args.output, "bamboo_config.yml"), "w+") as backupCfg:
                yaml.dump(config, backupCfg)

    #def prepareTree(self, tree, sample=None, sampleCfg=None, backend=None):
    def prepareTree(self, tree, sample=None, sampleCfg=None):
        era  = sampleCfg.get("era") if sampleCfg else None
        isMC = self.isMC(sample)

        if self.doNanoAODversion in ["v8", "v9"]:
            self.isULegacy = True
            metName   = "MET"
        else:
            self.isULegacy = False
            metName   = "METFixEE2017" if era == "2017" else "MET"
        
        from bamboo.treedecorators import NanoAODDescription, nanoRochesterCalc, nanoJetMETCalc, nanoJetMETCalc_METFixEE2017, CalcCollectionsGroups, nanoFatJetCalc
        nanoJetMETCalc_both = CalcCollectionsGroups(Jet=("pt", "mass"), systName="jet", changes={metName: (f"{metName}T1", f"{metName}T1Smear")}, **{metName: ("pt", "phi")})
        nanoJetMETCalc_data = CalcCollectionsGroups(Jet=("pt", "mass"), systName="jet", changes={metName: (f"{metName}T1",)}, **{metName: ("pt", "phi")})
        
        if self.doSaveQCDVars:
            qcd_variations = NanoAODHistoModule.prepareTree(self, tree, sample=sample, sampleCfg=sampleCfg, 
                                            description=NanoAODDescription.get("v7", year=(era if "VFP" not in era else "2016"), isMC=isMC, systVariations=[]),
                                            backend=self.args.backend )[1]
        
        if self.doMETT1Smear: nanoJetMETCalc_var = nanoJetMETCalc_both if isMC else nanoJetMETCalc_data
        else: nanoJetMETCalc_var = nanoJetMETCalc
        
        if self.isULegacy: nanoJetMETCalc_ = nanoJetMETCalc_var
        else: nanoJetMETCalc_ = nanoJetMETCalc_METFixEE2017 if era == "2017" else nanoJetMETCalc_var
        
        tree,noSel,be,lumiArgs = NanoAODHistoModule.prepareTree(self, tree, sample=sample, sampleCfg=sampleCfg, 
        #tree,noSel,be,lumiArgs = NanoAODHistoModule.prepareTree(self, tree, sample=sample, sampleCfg=sampleCfg, backend=backend,
                                            description=NanoAODDescription.get("v7", year=(era if "VFP" not in era else "2016"), 
                                                                               isMC=isMC, systVariations=[ nanoRochesterCalc, nanoJetMETCalc_, nanoFatJetCalc ]),
                                            backend=self.args.backend ) 
        
        
        #############################################################
        # Ellipses :
        #############################################################
        loadHeader(os.path.abspath(os.path.join(zaPath, "include/masswindows.h")))

        ellipsesName = be.symbol("std::vector<MassWindow> <<name>>{{}}; // for {0}".format(sample), nameHint="hza_ellipses{0}".format("".join(c for c in sample if c.isalnum())))
        ellipses_handle = getattr(gbl, ellipsesName)
        self.ellipses = op.extVar("std::vector<MassWindow>", ellipsesName) ## then use self.ellipses.at(i).radius(...) in your code

        with open("/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZATools/scripts_ZA/ellipsesScripts/vers20.06.03Inputs/fullEllipseParamWindow_MuMu.json") as ellPF:
            self.ellipse_params = json.load(ellPF)
        
        for params in self.ellipse_params:
            xc, yc, a, b, theta, MA, MH = params
            M11 = math.cos(theta)/math.sqrt(a)
            M12 = math.sin(theta)/math.sqrt(a)
            M21 = -math.sin(theta)/math.sqrt(b)
            M22 = math.cos(theta)/math.sqrt(b)
            ellipses_handle.push_back(gbl.MassWindow(xc, yc, M11, M12, M21, M22))
        #############################################################
        
        isNotWorker = (self.args.distributed != "worker") 
        era_ = era if "VFP" not in era else "2016"
        
        roccor = {'2016-preVFP' : "RoccoR2016aUL.txt", 
                  '2016-postVFP': "RoccoR2016bUL.txt",
                  '2017'        : "RoccoR2017UL.txt",
                  '2018'        : "RoccoR2018UL.txt"
                  }
        configureRochesterCorrection(tree._Muon, os.path.join(os.path.dirname(__file__), "data/roccor.Run2.v5", roccor[era]), isMC=isMC, backend=be, uName=sample)
        
        #############################################################
        ## Configure Jet Energy corrections and Jets Energy resolutions 
        # JEC's Recommendation for Full RunII: https://twiki.cern.ch/twiki/bin/view/CMS/JECDataMC
        # JER : -----------------------------: https://twiki.cern.ch/twiki/bin/view/CMS/JetResolution
        # list of supported para in JER : https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookJetEnergyResolution#List_of_supported_parameters 
        # github : https://github.com/cms-jet/JRDatabase/tree/master/textFiles
        ## Configure Type 1 MET corrections
        # if self.doJES =="merged": This is the full list, use enableSystematics –to filter systematics variations
        #['Absolute', f'Absolute_{era_}', 'BBEC1', f'BBEC1_{era_}', 'EC2', f'EC2_{era_}', 'FlavorQCD', 'HF', f'HF_{era_}', 'RelativeBal', f'RelativeSample_{era_}']
        #############################################################
        if self.isMC(sample):
            if self.doJES =="merged":
                jesUncertaintySources = "Merged"
            elif self.doJES == "total":
                jesUncertaintySources = ["Total"]
            elif self.doJES == "all":
                jesUncertaintySources = "All"

            JECs = {'2016-preVFP' : "Summer19UL16APV_V7_MC",
                    '2016-postVFP': "Summer19UL16_V7_MC",
                    '2017'        : "Summer19UL17_V5_MC", 
                    '2018'        : "Summer19UL18_V5_MC"
                    }
            
            JERs = {'2016-preVFP' : "Summer20UL16APV_JRV3_MC", 
                    '2016-postVFP': "Summer20UL16_JRV3_MC",
                    '2017'        : "Summer19UL17_JRV3_MC",
                    '2018'        : "Summer19UL18_JRV2_MC"
                    }
        else:
            jesUncertaintySources = None
            JECs = {'2016-preVFP' : "Summer19UL16APV_RunBCDEF_V7_DATA", 
                    '2016-postVFP': "Summer19UL16_RunFGH_V7_DATA", 
                    '2017'        : "Summer19UL17_RunBCDEF_V5_DATA",
                    '2018'        : "Summer19UL18_V5_DATA",
                    }
            
            JERs = {'2016-preVFP' : "Summer20UL16APV_JRV3_DATA", 
                    '2016-postVFP': "Summer20UL16_JRV3_DATA", 
                    '2017'        : "Summer19UL18_JRV2_DATA",
                    '2018'        : "Summer19UL18_JRV2_DATA",
                    }
        
        cmJMEArgs = {
                "jec": JECs[era],
                "smear": JERs[era],
                "splitJER": self.doSplitJER,
                "jesUncertaintySources": jesUncertaintySources,
                #"jecLevels":[], #  default : L1FastJet, L2Relative, L3Absolute, and also L2L3Residual for data
                "regroupTag": "V2",
                "addHEM2018Issue": (era == "2018"),
                "mayWriteCache": isNotWorker,
                "isMC": isMC,
                "backend": be,
                "uName": sample
                }

        # just apply to data
        if isMC:
            configureJets(tree._Jet, "AK4PFchs", **cmJMEArgs)
            configureJets(tree._FatJet, "AK8PFPuppi", mcYearForFatJets=(era if "VFP" not in era else "2016"), **cmJMEArgs)
            
            if self.doMETT1Smear: 
                configureType1MET(getattr(tree, f"_{metName}T1Smear"), isT1Smear=True, **cmJMEArgs)
            else:
                configureType1MET(getattr(tree, f"_{metName}"), **cmJMEArgs)
        
        #del cmJMEArgs["uName"]
        #configureType1MET(getattr(tree, f"_{metName}T1"), enableSystematics=((lambda v : not v.startswith("jer")) if isMC else None), uName=f"{sample}NoSmear", **cmJMEArgs)
        
        #############################################################
        # triggers path 
        #############################################################
        triggersPerPrimaryDataset = {}
        if "2016" in era:
            triggersPerPrimaryDataset = {
                "DoubleMuon" : [ tree.HLT.Mu17_TrkIsoVVL_Mu8_TrkIsoVVL,
                                 tree.HLT.Mu17_TrkIsoVVL_Mu8_TrkIsoVVL_DZ,
                                 tree.HLT.Mu17_TrkIsoVVL_TkMu8_TrkIsoVVL,
                                 tree.HLT.Mu17_TrkIsoVVL_TkMu8_TrkIsoVVL_DZ ],
                "DoubleEG"   : [ tree.HLT.Ele23_Ele12_CaloIdL_TrackIdL_IsoVL_DZ,
                                 tree.HLT.Ele23_Ele12_CaloIdL_TrackIdL_IsoVL ],  # DZ double electron (loosely isolated)
                "MuonEG"     : [ tree.HLT.Mu23_TrkIsoVVL_Ele12_CaloIdL_TrackIdL_IsoVL ],
                "SingleMuon" : [ tree.HLT.IsoMu24,
                                 tree.HLT.IsoTkMu24,
                                 #tree.HLT.Mu50,
                                ],
                "SingleElectron": [tree.HLT.Ele27_WPTight_Gsf],
                }
            
            if "2016F_UL16postVFP" in sample or "2016G" in sample or "2016H" in sample:
                triggersPerPrimaryDataset["MuonEG"] += [ 
                        ## added for eras post VFP : F, G, H and removed for all preVFP samples
                        tree.HLT.Mu12_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL_DZ, 
                        tree.HLT.Mu12_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL,
                        tree.HLT.Mu8_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL_DZ,
                        tree.HLT.Mu23_TrkIsoVVL_Ele12_CaloIdL_TrackIdL_IsoVL_DZ]
            
            if "2016H" not in sample :
                triggersPerPrimaryDataset["MuonEG"] += [ 
                        ## removed for era H
                        tree.HLT.Mu8_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL]
            
            if "2016H" in sample:
                triggersPerPrimaryDataset["DoubleMuon"] += [ 
                        tree.HLT.TkMu17_TrkIsoVVL_TkMu8_TrkIsoVVL, 
                        tree.HLT.TkMu17_TrkIsoVVL_TkMu8_TrkIsoVVL_DZ ]

            #if "2016B_ver1" not in sample :
            #    triggersPerPrimaryDataset["SingleMuon"] += [ 
                        ## removed for era B ver1
            #            tree.HLT.TkMu50]

        elif era == "2017":
            # https://twiki.cern.ch/twiki/bin/view/CMS/MuonHLT2017
            triggersPerPrimaryDataset = {
                "DoubleMuon" : [ #tree.HLT.Mu17_TrkIsoVVL_Mu8_TrkIsoVVL,  # this one is prescaled 
                                 tree.HLT.Mu17_TrkIsoVVL_Mu8_TrkIsoVVL_DZ,
                                 tree.HLT.Mu17_TrkIsoVVL_Mu8_TrkIsoVVL_DZ_Mass8,
                                 ],
                # it's recommended to not use the DoubleEG HLT _ DZ version  for 2017 and 2018, 
                # using them it would be a needless efficiency loss !
                #---> https://twiki.cern.ch/twiki/bin/view/CMS/EgHLTRunIISummary
                "DoubleEG"   : [ tree.HLT.Ele23_Ele12_CaloIdL_TrackIdL_IsoVL, # loosely isolated
                                 #tree.HLT.DoubleEle33_CaloIdL_MW,
                                 ], 
                                 # the MW refers to the pixel match window being "medium window" working point
                                 # also require additional HLT Zvtx Efficiency Scale Factor 
                "MuonEG"     : [ 
                                 tree.HLT.Mu23_TrkIsoVVL_Ele12_CaloIdL_TrackIdL_IsoVL_DZ,
                                 tree.HLT.Mu12_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL_DZ,
                                 tree.HLT.Mu8_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL_DZ ],
                # FIXME : if you want to include them need to include the primary dataset too !!
                #"SingleElectron": [ tree.HLT.Ele35_WPTight_Gsf,
                #                    tree.HLT.Ele28_eta2p1_WPTight_Gsf_HT150 ],
                #"SingleMuon"    : [ tree.HLT.IsoMu27,
                #                    tree.HLT.IsoMu24_eta2p1],
            }
            
            if "2017B" not in sample:
             ## all are removed for 2017 era B
                triggersPerPrimaryDataset["MuonEG"] += [ 
                        tree.HLT.Mu23_TrkIsoVVL_Ele12_CaloIdL_TrackIdL_IsoVL,
                        tree.HLT.Mu12_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL,
                        #tree.HLT.Mu8_TrkIsoVVL_Ele23_CaloIdL_TrackIdL_IsoVL  : prescaled 
                        ]
                triggersPerPrimaryDataset["DoubleMuon"] += [ 
                        tree.HLT.Mu17_TrkIsoVVL_Mu8_TrkIsoVVL_DZ_Mass3p8 ]
                triggersPerPrimaryDataset["DoubleEG"] += [ 
                        tree.HLT.DiEle27_WPTightCaloOnly_L1DoubleEG ]
            #if "2017B" not in sample and "2017C" not in sample:
            #    triggersPerPrimaryDataset["DoubleEG"] += [ 
            #            tree.HLT.DoubleEle25_CaloIdL_MW ]
        elif era == "2018":
            suffix    = "_UL" if self.isULegacy else "_"
            eraInYear = "" if isMC else next(tok for tok in sample.split(suffix) if tok.startswith(era))[4:]
            triggersPerPrimaryDataset = corr.catchHLTforSubPrimaryDataset(era, eraInYear, tree, isMC=isMC)
        
        #############################################################
        if self.isMC(sample):
            # remove double counting passing TTbar Inclusive + TTbar Full Leptonic ==> mainly for 2016 Analysis 
            sampleCut = None
            if sample == "TT":
                genLeptons_hard = op.select(tree.GenPart, 
                                            lambda gp : op.AND((gp.statusFlags & (0x1<<7)), 
                                                                op.in_range(10, op.abs(gp.pdgId), 17)))
                sampleCut = (op.rng_len(genLeptons_hard) <= 2)
                noSel = noSel.refine("genWeight", weight=tree.genWeight, 
                                                  cut=[sampleCut, op.OR(*chain.from_iterable(triggersPerPrimaryDataset.values())) ], 
                                                  autoSyst=self.doSysts)
            else:
                noSel = noSel.refine("genWeight", weight=tree.genWeight, 
                                                cut=(op.OR(*chain.from_iterable(triggersPerPrimaryDataset.values()))), 
                                                autoSyst=self.doSysts)
            if self.doSysts:
                logger.info("Adding QCD scale variations, ISR, FSR and PDFs uncertainties")
                noSel = utils.addTheorySystematics(self, sample, sampleCfg, tree, noSel, qcdScale=True, PSISR=True, PSFSR=True, PDFs=True, pdf_mode=self.pdfVarMode)
            if self.doSaveQCDVars:
                qcd_variations = utils.addTheorySystematics(self, sample, sampleCfg, tree, qcd_variations, qcdScale=True, PSISR=False, PSFSR=False, PDFs=False, pdf_mode=self.pdfVarMode)
        else:
            noSel = noSel.refine("withTrig", cut=(makeMultiPrimaryDatasetTriggerSelection(sample, triggersPerPrimaryDataset)))
        
        return tree,noSel,be,lumiArgs
  

    def defineObjects(self, t, noSel, sample=None, sampleCfg=None):
        from bamboo.analysisutils import forceDefine
        from bamboo.plots import Skim
        from bamboo.plots import EquidistantBinning as EqB
        from bamboo import treefunctions as op
        
        from bambooToOls import Plot
        from METFilter_xyCorr import METFilter, METcorrection, ULMETXYCorrection

        def getIDX(wp = None):
            return (0 if wp=="L" else ( 1 if wp=="M" else 2))
        
        def getOperatingPoint(wp = None):
            return ("Loose" if wp == 'L' else ("Medium" if wp == 'M' else "Tight"))
        
        def getformattedERA(era):
            return "UL"+era.replace("-", "").replace("20", "")

        plots = []
        
        isMC = self.isMC(sample)
        era  = sampleCfg.get("era") if sampleCfg else None
        era_ = era if "VFP" not in era else "2016"
        
        ##################################################
        # MET filter flags 
        ##################################################
        noSel = noSel.refine("passMETFlags", cut=METFilter(t.Flag, era, isMC) )
        
        ##################################################
        # Pileup 
        ##################################################
        if self.isMC(sample):
            self.PUWeight = corr.makePUWeight(t, era, noSel)
            noSel = noSel.refine("puWeight", weight=corr.makePUWeight(t, era, noSel))
        
        ##################################################
        # Top pt reweighting 
        ##################################################
        if self.doTop_reweighting:
            noSel, plt = corr.Top_reweighting(t, noSel, sampleCfg, isMC)
            #plots.extend(plt)
        
        ###############################################
        # Muons ID , ISO and RECO cuts and scale factors 
        # Working Point for 2016- 2017 -2018 : medium-identification  and tight-isolation 
        # https://twiki.cern.ch/twiki/bin/view/CMS/SWGuideMuonIdRun2#Muon_Isolation
        ###############################################
        forceDefine(t._Muon.calcProd, noSel)
        
        #To suppress nonprompt leptons, the impact parameter in three dimensions of the lepton track, with respect to the primaryvertex, is required to be less than 4 times its uncertainty (|SIP3D|<4)
        sorted_muons = op.sort(t.Muon, lambda mu : -mu.pt)
        muons = op.select(sorted_muons, lambda mu : op.AND(mu.pt > 15., op.abs(mu.eta) < 2.4, mu.mediumId, mu.pfRelIso04_all<0.15, op.abs(mu.sip3d) < 4.))

        muMediumIDSF = corr.getScaleFactor(era, noSel, "muon_ID", "muid_medium", defineOnFirstUse=True)
        muTightIsoSF = corr.getScaleFactor(era, noSel, "muon_iso", "muiso_tight", defineOnFirstUse=True)
        
        def muTriggerSF(mu, era):
            pt_range= {"2016-preVFP" : 26., 
                       "2016-postVFP": 26.,
                       "2017": 29.,
                       "2018": 26., }
            sf = corr.getScaleFactor(era, noSel, "muon_trigger", "mu_trigger", defineOnFirstUse=False)
            #if not '2016' in era:
            return op.switch( mu.pt >= pt_range[era] , sf(mu), op.c_float(1.))
            #else:
            #    return op.c_float(1.)
        ###############################################
        # Electrons : ID , RECO cuts and scale factors
        # Wp  // 2016: Electron_cutBased_Sum16==3  -> medium     // 2017 -2018  : Electron_cutBased ==3   --> medium ( Fall17_V2)
        # asking for electrons to be in the Barrel region with dz<1mm & dxy< 0.5mm   //   Endcap region dz<2mm & dxy< 0.5mm 
        # cut-based ID Fall17 V2 the recommended one from POG for the FullRunII
        ###############################################
        sorted_electrons = op.sort(t.Electron, lambda ele : -ele.pt)
        electrons = op.select(sorted_electrons, lambda ele : op.AND(ele.pt > 15., op.abs(ele.eta) < 2.5 , ele.cutBased>=3, op.abs(ele.sip3d) < 4., 
                                                                    op.OR(op.AND(op.abs(ele.dxy) < 0.05, op.abs(ele.dz) < 0.1), 
                                                                          op.AND(op.abs(ele.dxy) < 0.05, op.abs(ele.dz) < 0.2) ))) 
        def scalefactor(wp):
            pt_thresh = {  "full"         : (lambda el: el.pt ), 
                         # "RecoAbove20"  : (lambda el: op.max(el.pt, 20.)),
                         # "RecoBelow20"  : (lambda el: op.min(el.pt, np.nextafter(20., -np.inf, dtype="float32")) ),
                         # "RecoBelow20"  : (lambda el: op.min(el.pt, 19.999998092651367) ), 
                         # "RecoBelow20"  : (lambda el: op.min(el.pt, op.c_float(np.nextafter(20., -np.inf, dtype="float32"))) )
                        }
            
            systName = "highpt_ele_reco" if wp == "RecoAbove20" else "lowpt_ele_reco"
            return corr.getScaleFactor(era, noSel, "electron_reco", systName, pt_=pt_thresh['full'], wp=wp, defineOnFirstUse=False) 
             
        def elRecoSF(el):
            return op.switch( el.pt < 20. , scalefactor('RecoBelow20')(el), scalefactor('RecoAbove20')(el))
        
        elMediumIDSF = corr.getScaleFactor(era, noSel, "electron_ID", "elid_medium", pt_=lambda el: el.pt, wp="Medium", defineOnFirstUse=True)
        
        ###############################################
        # Trigger scale factors
        ###############################################
        def localizeHLTSF(era, channel):
            newEra   = 'UL'+era.split('-')[0]
            suffix   = era.replace('-','').replace('20','')
            fileName = f"dilep_trig_sf_UL{suffix}.json.gz"
            path = os.path.join("/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/data/HLTefficiencies/run2ULegacyHLT/", newEra, fileName)
            
            lepflav = "ee" if channel == "ElEl" else "mumu" if channel =="MuMu" else "emu"
            correction = f"h2D_SF_{lepflav}_lepABpt_FullError"
            return path , correction

        def getHLT_scalefactors(ll, era, channel):
            fileName , correction = localizeHLTSF(era, channel)
            systName = f"{channel.lower()}_trigSF"
            
            if channel != "MuEl":
                params = {"lep1_pt": lambda ll: ll[0].pt, "lep2_pt": lambda ll: ll[1].pt}
            else:
                params = {"mu_pt"  : lambda ll: ll[0].pt, "ele_pt" : lambda ll: ll[1].pt}
            
            sf = get_correction(fileName, correction, params=params, systParam="ValType", systNomName="sf",
                                    systVariations={f"{systName}up": "sfup", f"{systName}down": "sfdown"},
                                    defineOnFirstUse=True, sel=noSel)
            return sf(ll)

        def getTkMu50_scalefactors(mu, era, channel):
            systName = f"{channel.lower()}_TkMu50_trigSF"
            TkMu50_SF = corr.getScaleFactor(era, noSel, "TkMu50_muon_trigger", systName, defineOnFirstUse=False)
            if era =='2018' or '2016' in era:
                return op.switch( mu.pt >= 52. , TkMu50_SF(mu), op.c_float(1.))
            else:
                return op.c_float(1.)
        ###############################################
        # MET xy correction  
        ###############################################
        MET = t.MET if self.isULegacy else (t.MET if era != "2017" else (t.METFixEE2017))
        PuppiMET = t.PuppiMET 
        if self.isULegacy:
            corrMET = ULMETXYCorrection(MET,t.PV,sample,f"UL{era_}",self.isMC(sample))
        else:
            corrMET = METcorrection(MET,t.PV,sample,era,self.isMC(sample))
        
        ###############################################
        # AK4 Jets selections
        # 2016 - 2017 - 2018   ( j.jetId &2) ->      tight jet ID
        # For 2017 data, there is the option of "Tight" or "TightLepVeto", 
        # depending on how much you want to veto jets that overlap with/are faked by leptons
        ###############################################
        deltaR = 0.4
        eta    = 2.4 if '2016' in era else 2.5
        pt     = 20. if '2016' in era else 30.

        jet_ID = { '2016-preVFP' : lambda j : j.jetId & 2, # tight
                   '2016-postVFP': lambda j : j.jetId & 2,
                   '2017'        : lambda j : j.jetId & 4,
                   '2018'        : lambda j : j.jetId & 4}
        
        puIdWP = "loose"
        jet_puID = { "loose"   : lambda j : j.puId & 0x4,
                     "medium"  : lambda j : j.puId & 0x2,
                     "tight"   : lambda j : j.puId & 0x1 }
        
        sorted_AK4jets= op.sort(t.Jet, lambda j : -j.pt)
        ###############################################
        # Apply Jet Plieup ID 
        #https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookNanoAOD
        # Jet ID flags bit1 is loose (always false in 2017 and 2018 since it does not exist), bit2 is tight, bit3 is tightLepVeto
        #jet.Id==6 means: pass tight and tightLepVeto ID. 
    
        #https://twiki.cern.ch/twiki/bin/viewauth/CMS/PileupJetID
            
        #puId==0 means 000: fail all PU ID;
        #puId==4 means 100: pass loose ID, fail medium, fail tight;  
        #puId==6 means 110: pass loose and medium ID, fail tight; 
        #puId==7 means 111: pass loose, medium, tight ID.
        ###############################################
        if self.CleanJets_fromPileup :
            AK4jetsSel = op.select(sorted_AK4jets, lambda j : op.AND(j.pt > pt, op.abs(j.eta) < eta, jet_ID[era], op.switch(j.pt < 50, j.puId & 0x4, op.c_bool(True)))) 
        else:    
            AK4jetsSel = op.select(sorted_AK4jets, lambda j : op.AND(j.pt > pt, op.abs(j.eta) < eta, (jet_ID[era])))        
        
        # exclude from the jetsSel any jet that happens to include within its reconstruction cone a muon or an electron.
        AK4jets = op.select(AK4jetsSel, 
                            lambda j : op.AND(
                                            op.NOT(op.rng_any(electrons, lambda ele : op.deltaR(j.p4, ele.p4) < deltaR )), 
                                            op.NOT(op.rng_any(muons, lambda mu : op.deltaR(j.p4, mu.p4) < deltaR ))))

        
        AK4jets_noptcutSel = op.select(sorted_AK4jets, lambda j : op.AND(op.abs(j.eta) < eta, jet_ID[era]))
        AK4jets_noptcut    = op.select(AK4jets_noptcutSel, 
                            lambda j : op.AND(
                                            op.NOT(op.rng_any(electrons, lambda ele : op.deltaR(j.p4, ele.p4) < deltaR )), 
                                            op.NOT(op.rng_any(muons, lambda mu : op.deltaR(j.p4, mu.p4) < deltaR ))))
        pu_weight = None
        if self.CleanJets_fromPileup and self.isMC(sample):
            pu_weight = corr.makePUIDSF(AK4jets, era_, wp=puIdWP[0].upper(), wpToCut=jet_puID[puIdWP])
            noSel = noSel.refine('Pileup_reweighting', weight= pu_weight )

        self.cleaned_AK4JetsByDeepFlav = op.sort(AK4jets, lambda j: -j.btagDeepFlavB)
        self.cleaned_AK4JetsByDeepB    = op.sort(AK4jets, lambda j: -j.btagDeepB)
        ###############################################
        # AK8 Boosted Jets 
        # ask for two subjet to be inside the fatjet
        # The AK8 jets are required to have the nsubjettiness parameters tau2/tau1< 0.5 
        # to be consistent with an AK8 jet having two subjets.
        ###############################################
        tau21 = { '2016': 0.7,
                  '2017': 0.7,
                  '2018': 0.65 }
        
        sorted_AK8jets = op.sort(t.FatJet, lambda j : -j.pt)
        AK8jetsSel = op.select(sorted_AK8jets, 
                                lambda j : op.AND(j.pt > 200., op.abs(j.eta) < 2.4, (j.jetId &2), 
                                                  j.subJet1.isValid,
                                                  j.subJet2.isValid,
                                                  j.tau2/j.tau1 < tau21[era_] ))
        AK8jets = op.select(AK8jetsSel, 
                            lambda j : op.AND(
                                            op.NOT(op.rng_any(electrons, lambda ele : op.deltaR(j.p4, ele.p4) < 0.8 )), 
                                            op.NOT(op.rng_any(muons, lambda mu : op.deltaR(j.p4, mu.p4) < 0.8 ))))
        
        self.cleaned_AK8JetsByDeepB = op.sort(AK8jets, lambda j: -j.btagDeepB)
        
        # No tau2/tau1 cut 
        fatjetsel_nosubjettinessCut = op.select(sorted_AK8jets, 
                                                    lambda j : op.AND(j.pt > 200., op.abs(j.eta) < 2.5, (j.jetId &2), 
                                                                      j.subJet1.isValid,
                                                                      j.subJet2.isValid) )
        
        fatjets_nosubjettinessCut = op.select(fatjetsel_nosubjettinessCut, 
                                                    lambda j : op.AND(
                                                        op.NOT(op.rng_any(electrons, lambda ele : op.deltaR(j.p4, ele.p4) < 0.8 )), 
                                                        op.NOT(op.rng_any(muons, lambda mu : op.deltaR(j.p4, mu.p4) < 0.8 ))))
        
        cleaned_fatjet = op.sort(fatjets_nosubjettinessCut, lambda j: -j.btagDeepB)

        ###############################################
        # btagging requirements :
        # Now,  let's ask for the jets to be a b tagged b-jets 
        # DeepCSV or DeepJet==DeepFlavour medium b-tagging working point
        # bjets ={ "DeepFlavour": {"L": ( pass loose, fail medium, fail tight), 
        #                          "M": ( pass loose, pass medium  fail tight), 
        #                          "T": ( pass tight, fail medium, fail loose)}     
        #          "DeepCSV"    : {"L": (  ----  you get the idea           ;), 
        #                          "M": (  ----                              ), 
        #                          "T": (  ----                              )} }
        ###############################################
        
        
        jetType = { 'resolved': { 
                        'DeepCSV': {
                            'jet':  self.cleaned_AK4JetsByDeepFlav, 
                            'workingPoints': ['L', 'M', 'T']       },
                        'DeepFlavour': { 
                            'jet':  self.cleaned_AK4JetsByDeepFlav,
                            'workingPoints': ['L', 'M', 'T']       } },
                    'boosted'  : { 
                        'DeepCSV': { 
                            'workingPoints': ['L', 'M'], 
                            'jet': self.cleaned_AK8JetsByDeepB     } }
                    }
        
        tagged_jets = {}
        for region in ['resolved', 'boosted']:
            tagged_jets[region] = {}

            for flav in ['b']:#, 'light']:
                tagged_jets[region][flav] = {}
                
                for tagger in jetType[region].keys():
                    tagged_jets[region][flav][tagger] = {}
                                
                    for wp in sorted(jetType[region][tagger]['workingPoints']):
        
                        idx = getIDX(wp)
                        if region == 'resolved':
                            wpdiscr_cut = corr.legacy_btagging_wpdiscr_cuts[tagger][era][idx]
                        else:
                            wpdiscr_cut = corr.BoostedTopologiesWP[tagger][era][wp]

                        subjets_btag_req = corr.get_subjets_requirements(tagger, wp, wpdiscr_cut, era)
                        print(f"::: {flav} flavour {region}-{tagger}{wp}, discriminator_cut = {wpdiscr_cut}" )
                        
                        lambdas = {
                            "resolved": {
                                "DeepFlavour": { 
                                    'b'    : (lambda j : j.btagDeepFlavB >= wpdiscr_cut),
                                    'light': (lambda j : j.btagDeepFlavB < wpdiscr_cut) }, 
                                "DeepCSV": {
                                    'b'    : (lambda j : j.btagDeepB >= wpdiscr_cut ),
                                    'light': (lambda j : j.btagDeepB < wpdiscr_cut ) } },
                            "boosted": {
                                "DeepCSV": {
                                    'b'    : subjets_btag_req['b'].get(self.doPassNbr_subjets+'_pass'),
                                    'light': subjets_btag_req['light'].get(self.doPassNbr_subjets+'_notpass') }}
                                }

                        passed_jets = op.select(jetType[region][tagger]['jet'], lambdas[region][tagger][flav])
                        tagged_jets[region][flav][tagger][wp] = passed_jets
        
        bjets_boosted  = tagged_jets['boosted']['b']
        bjets_resolved = tagged_jets['resolved']['b']

        ########################################################
        # DY reweighting 
        ########################################################
         
        if self.dotthDY_reweighting and isMC and "group" in sampleCfg.keys() and sampleCfg["group"]=='DY' and not sample.startswith('DYJetsToLL_M-10to50'):
            noSel = corr.DrellYanreweighting(noSel, self.cleaned_AK4JetsByDeepFlav, 'DeepFlavour', era, self.doSysts)
        
        ########################################################
        # Zmass reconstruction : Opposite Sign , Same Flavour leptons
        ########################################################
        # supress quaronika resonances and jets misidentified as leptons
        LowMass_cut = lambda lep1, lep2: op.invariant_mass(lep1.p4, lep2.p4)>12.
        # Dilepton selection: opposite sign leptons in range 70.<mll<120. GeV 
        osdilep_Z   = lambda lep1,lep2 : op.AND(lep1.charge != lep2.charge, op.in_range(70., op.invariant_mass(lep1.p4, lep2.p4), 110.))
        osdilep     = lambda lep1,lep2 : op.AND(lep1.charge != lep2.charge)
        
        osLLRng = {
                "MuMu" : op.combine(muons, N=2, pred= osdilep_Z),
                "ElEl" : op.combine(electrons, N=2, pred= osdilep_Z),
               #"ElMu" : op.combine((electrons, muons), pred=lambda ele,mu : op.AND(LowMass_cut(ele, mu), osdilep(ele, mu) , ele.pt > mu.pt )),
               #"MuEl" : op.combine((muons, electrons), pred=lambda mu,ele : op.AND(LowMass_cut(mu, ele), osdilep(mu, ele), mu.pt > ele.pt )),
                "MuEl" : op.combine((muons, electrons), pred=lambda mu,ele : op.AND(LowMass_cut(mu, ele), osdilep(mu, ele))),
               #"2OSSFLep" : op.AND(op.combine(muons, N=2, pred= osdilep_Z), op.combine(electrons, N=2, pred= osdilep_Z)) 
                }
         
        hasOSLL_cmbRng = lambda cmbRng : op.AND(op.rng_len(cmbRng) > 0, cmbRng[0][0].pt > 25.) 
        
        ## helper selection (OR) to make sure jet calculations are only done once
        hasOSLL = noSel.refine("hasOSLL", cut=op.OR(*( hasOSLL_cmbRng(rng) for rng in osLLRng.values())))
        
        if self.isMC(sample):
            forceDefine(t._Jet.calcProd, hasOSLL)
        
        ########################################################
        metName = ("MET" if self.isULegacy else ("MET" if era != "2017" else "METFixEE2017"))
        if self.isMC(sample):
            if self.doMETT1Smear:
                forceDefine(getattr(t, f"_{metName}T1Smear").calcProd, hasOSLL)
                #forceDefine(getattr(t, f"_{metName}T1").calcProd, hasOSLL)
            else:
                forceDefine(getattr(t, f"_{metName}").calcProd, hasOSLL)

        ########################################################
        # https://lathomas.web.cern.ch/lathomas/TSGStuff/L1Prefiring/PrefiringMaps_2016and2017/
        # https://twiki.cern.ch/twiki/bin/view/CMS/L1PrefiringWeightRecipe#Introduction
        # NANOAOD: The event weights produced by the latest version of the producer are included in nanoAOD starting from version V9. 
        # Lower versions include an earlier version of the ECAL prefiring weight and do not include the muon weights!
        ########################################################
        mumu_sf = []
        elmu_sf = []
        muel_sf = []
        elel_sf = []
        if isMC:
            if era != '2018':
                mumu_sf.append(corr.getL1PreFiringWeight(t))
                elmu_sf.append(corr.getL1PreFiringWeight(t))
                muel_sf.append(corr.getL1PreFiringWeight(t))
                elel_sf.append(corr.getL1PreFiringWeight(t))
            if era == '2017':
                HLTZvtx = op.systematic(op.c_float(0.991), name='HLTZvtx', up=op.c_float(0.992), down=op.c_float(0.990))
                elmu_sf.append(HLTZvtx)
                muel_sf.append(HLTZvtx)
                elel_sf.append(HLTZvtx)

            llSFs = { "MuMu" : (lambda ll : 
                        [ muMediumIDSF(ll[0]), muMediumIDSF(ll[1]), 
                          muTightIsoSF(ll[0]), muTightIsoSF(ll[1]), 
                          getHLT_scalefactors(ll, era, "MuMu"), muTriggerSF(ll[0], era), muTriggerSF(ll[1], era), 
                          #getTkMu50_scalefactors(ll[0], era, 'MuMu'),  getTkMu50_scalefactors(ll[1], era, 'MuMu'),
                          ] + mumu_sf ),
                    #"ElMu" : (lambda ll : 
                    #  [ elMediumIDSF(ll[0]), elRecoSF(ll[0]),
                    #    muMediumIDSF(ll[1]), muTightIsoSF(ll[1]), 
                    #    getHLT_scalefactors(ll, era, "ElMu"), muTriggerSF(ll[1], era)
                    #    ] + elmu_sf ),
                    "MuEl" : (lambda ll : 
                        [ muMediumIDSF(ll[0]), muTightIsoSF(ll[0]),
                          elMediumIDSF(ll[1]), elRecoSF(ll[1]), 
                          getHLT_scalefactors(ll, era, "MuEl"), muTriggerSF(ll[0], era), 
                          #getTkMu50_scalefactors(ll[0], era, 'MuMu'),
                          ] + muel_sf ),                                       
                    "ElEl" : (lambda ll :
                        [ elMediumIDSF(ll[0]), elMediumIDSF(ll[1]), 
                          elRecoSF(ll[0]), elRecoSF(ll[1]), 
                          getHLT_scalefactors(ll, era, "ElEl"), 
                          ] + elel_sf )
                    }

        categories = dict( (channel, (catLLRng[0], 
                                      hasOSLL.refine("hasOs{0}".format(channel), 
                                                        cut=hasOSLL_cmbRng(catLLRng), 
                                                        weight=(llSFs[channel](catLLRng[0]) if isMC else None) 
                                                        ) 
                                      )) for channel, catLLRng in osLLRng.items())
        
        return noSel, plots, categories, AK4jets, AK8jets, fatjets_nosubjettinessCut, bjets_resolved, bjets_boosted, electrons, muons, MET, corrMET, PuppiMET



class NanoHtoZA(NanoHtoZABase, HistogramsModule):
    def __init__(self, args):
        super(NanoHtoZA, self).__init__(args)
    #@profile
    # https://stackoverflow.com/questions/276052/how-to-get-current-cpu-and-ram-usage-in-python
    def definePlots(self, t, noSel, sample=None, sampleCfg=None):
        from bamboo.plots import VariableBinning as VarBin
        from bamboo.plots import Skim, CutFlowReport
        
        from bambooToOls import Plot
        from reweightDY import prepareCP_ForDrellYan0Btag, ProduceFitPolynomialDYReweighting, getDYweightFromPolyfit
    
        def getIDX(wp = None):
            return (0 if wp=="L" else ( 1 if wp=="M" else 2))
        
        def getProduction(process):
            if process =='gg_fusion':
                return 'nb=2', 'ggH'
            elif process == 'bb_associatedProduction':
                return 'nb=3', 'bbH'

        def getOperatingPoint(wp = None):
            return ("Loose" if wp == 'L' else ("Medium" if wp == 'M' else "Tight"))
       
        def mass_to_str(m): 
            m = "%.2f"%m
            return str(m).replace('.','p')
        
        def inputStaticCast(inputDict,cast='float'):
            return [op.static_cast(cast,v) for v in inputDict.values()]
        
        noSel, plots, categories, AK4jets, AK8jets, fatjets_nosubjettinessCut, bjets_resolved, bjets_boosted, electrons, muons, MET, corrMET, PuppiMET = super(NanoHtoZA, self).defineObjects(t, noSel, sample, sampleCfg)
        
        era  = sampleCfg.get("era") if sampleCfg else None
        era_ = era if "VFP" not in era else "2016"
        
        yield_object = corr.makeYieldPlots()
        isMC = self.isMC(sample)
        isSignal = True if "type" in sampleCfg.keys() and sampleCfg["type"]=="signal" else (False)
        binScaling = 1 
        
        onnx__version = False
        tf__version   = False
       

        selections_for_cutflowreport = CutFlowReport("yields", recursive=True)
        plots.append(selections_for_cutflowreport)

        plots_ToSum  = collections.defaultdict(list)
        plots_ToSum2 = collections.defaultdict(list)
        
        addIncludePath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "include"))
        loadHeader("BTagEffEvaluator.h")
       
        run2_bTagEventWeight_PerWP = collections.defaultdict(dict)
        
        if self.doPass_bTagEventWeight and isMC:
            for wp in self.WorkingPoints:
                
                idx = getIDX(wp)
                systMapping = {}
                full_scheme = False
                decorr_eras = False # FIXME len(config['configuration']['eras'].keys())!=1 
                if self.doSysts:
                    full_scheme = False ## FIXME
                    systMapping = {}
                    __not_yet_systMapping = {
                            "pileup"  : "pileup", # correlated with "pileup" uncertainty
                            "isr"     : None,     # uncorrelated, standalone b-tag uncertainty
                            "fsr"     : None,
                            "hdamp"   : None,
                            "qcdscale": None,
                            "topmass" : None,
                            "type3"   : None,
                            "jes"     : None,
                            "jer0"    : "jer",
                            "jer1"    : "jer",
                        }
                
                run2_bTagEventWeight_PerWP[wp] = corr.makeBtagSF(self.cleaned_AK4JetsByDeepB, self.cleaned_AK4JetsByDeepFlav, self.cleaned_AK8JetsByDeepB, 
                                wp, idx, corr.legacy_btagging_wpdiscr_cuts, era, noSel, sample, self.dobJetER, self.doCorrect, isSignal,
                                defineOnFirstUse=False, decorr_eras=decorr_eras, full_scheme=full_scheme, full_scheme_mapping=systMapping)

        if self.doEvaluate:
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/tf_models/tf_bestmodel_max_eval_mean_trainResBoOv0_fbversion.pb'
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/scratch/ul__results/test__4/model/tf_bestmodel.pb"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/onnx_models/prob_model.onnx"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work__1/keras_tf_onnx_models/all_combined_dict_343_model.pb"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work__1/keras_tf_onnx_models/prob_model.onnx"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ML-Tools/keras_tf_onnx_models/prob_model_work__1.onnx"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__1/keras_tf_onnx_models/prob_model_work_nanov9__1.onnx"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__1/keras_tf_onnx_models/all_combined_dict_241_model.onnx"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__1/ext1/keras_tf_onnx_models/all_combined_dict_216_model.pb"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__5/keras_tf_onnx_models/all_combined_dict_432_model.pb"
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__15/keras_tf_onnx_models/all_combined_dict_397_model.onnx" 
            #ZAmodel_path = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__15/keras_tf_onnx_models/all_combined_dict_397_model.pb" 
            ZAmodel_path  = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/bamboo_/ZAMachineLearning/ul__results/work_nanov9__18/keras_tf_onnx_models/all_combined_resubmit_dict_102_model.pb"
            
            if not os.path.exists(ZAmodel_path):
                raise RuntimeError(f'Could not find model: {ZAmodel_path}')
            else:
                if ZAmodel_path.split('/')[-1].endswith('.onnx'):
                    onnx__version = True
                elif ZAmodel_path.split('/')[-1].endswith('.pb'):
                    tf__version   = True
            try:
                #===============================================================================
                # Tensorflow : The otherArgs keyword argument should be (inputNodeNames, outputNodeNames), 
                # where each of the two can be a single string, or an iterable of them.
                if tf__version:
                    outputs = 'Identity'
                    inputs  = ['l1_pdgId', 'era', 'bb_M', 'llbb_M', 'bb_M_squared','llbb_M_squared', 'bb_M_x_llbb_M', 'mA','mH', 'isResolved', 'isBoosted', 'isggH', 'isbbH']
                    ZA_mvaEvaluator = op.mvaEvaluator(ZAmodel_path,mvaType='Tensorflow',otherArgs=(inputs, outputs), nameHint='tf_ZAModel')
                #===============================================================================
                # ONNX : The otherArgs keyword argument should the name of the output node (or a list of those)
                elif onnx__version:
                    ZA_mvaEvaluator = op.mvaEvaluator(ZAmodel_path, mvaType='ONNXRuntime',otherArgs=("out"), nameHint='ONNX_ZAModel')
                #===============================================================================
            except Exception as ex:
                raise RuntimeError(f'-- {ex} -- when op.mvaEvaluator model: {ZAmodel_path}.')

            bayesian_blocks = "/home/ucl/cp3/kjaffel/bamboodev/ZA_FullAnalysis/ZAStatAnalysis/ul__combinedlimits/preapproval__6/rebinned_edges_bayesian_all.json"
            if not os.path.exists(bayesian_blocks):
                raise RuntimeError(f'Could not find model: {bayesian_blocks}')
            else:
                f = open(bayesian_blocks)
                bayesian_blocks_binnings = json.load(f)
            

    
        jlenOpts       = { "inclusive": 
                                    { "resolved": ' $\ge$ 2 ',
                                      "boosted" : ' $\ge$ 1 '},
                            "exclusive": {"ggH": 
                                             { "resolved": ' == 2 ' ,
                                               "boosted" : ' == 1 '},
                                          "bbH": 
                                             { "resolved": ' $\ge$ 3 ',
                                               "boosted" : ' $>$ 1 ' } 
                                        }
                        }

        optimizeMETcut  =  {"gg_fusion": 
                                { "resolved": 80.,
                                  "boosted" : 80. },
                            "bb_associatedProduction": 
                                { "resolved": 80.,
                                  "boosted" : 80. } 
                        }
        
        jetType        = { "resolved": "AK4",
                           "boosted" : "AK8"
                        }
            
        lljj_selName   = { "resolved": "has2Lep_atLeast_2ResolvedJets",
                           "boosted" : "has2Lep_atLeast_1BoostedFatJet"
                        }
            
        lljj_jets      = { "resolved": AK4jets,
                           "boosted" : AK8jets 
                        }
        
        lljj_cleanjets = { "resolved": { 'DeepFlavour': self.cleaned_AK4JetsByDeepFlav, 
                                         'DeepCSV'    : self.cleaned_AK4JetsByDeepB },
                           "boosted" : {'DeepCSV'     : self.cleaned_AK8JetsByDeepB } 
                        }
                
        lljj_bJets     = { "resolved": bjets_resolved,
                           "boosted" : bjets_boosted 
                        }
        if self.doDDBvsL: 
            lljj_bJets['boosted']["DeepDoubleBvLV2"] = {}

        masses_seen = [
        #part0 : 21 signal samples 
        #( MH, MA)
        ( 200, 50),
        ( 200, 100), ( 200, 125),
        ( 250, 50),  ( 250, 100),
        ( 300, 50),  ( 300, 100), ( 300, 200),
        ( 500, 50),  ( 500, 100), ( 500, 200), ( 500, 300), ( 500, 400), (510, 130),
        ( 650, 50),  ( 609.21, 253.68), 
        ( 750, 610), 
        ( 800, 50), ( 800, 100), ( 800, 200), ( 800, 400), ( 800, 700),
        (1000, 50), (1000, 200), (1000, 500),    
        ]
        #part1
        masses_notseen = [
        #( 173.52,  72.01),  
        #( 209.90,  30.00), ( 209.90,  37.34), ( 261.40, 102.99), ( 261.40, 124.53),
        #( 296.10, 145.93), ( 296.10,  36.79),
        #( 379.00, 205.76), 
        #( 442.63, 113.53), ( 442.63,  54.67),( 442.63,  80.03), 
        #( 609.21, 298.01), 
        #( 717.96,  30.00), ( 717.96, 341.02), 
        #( 846.11, 186.51), ( 846.11, 475.64), ( 846.11,  74.80), 
        #( 997.14, 160.17), ( 997.14, 217.19), ( 997.14, 254.82), ( 997.14, 64.24) 
        ]
        
        # basic distribution for control region
        make_ZpicPlots              = False
        make_JetsPlusLeptonsPlots   = True
        make_JetmultiplictyPlots    = False
        make_METPlots               = False
        make_METPuppiPlots          = False
        make_recoVerticesPlots      = False
        
        # One of these two at least should be "True" if you want to get the final sel plots (.ie. ll + bb )
        make_bJetsPlusLeptonsPlots_METcut   = True
        make_bJetsPlusLeptonsPlots_NoMETcut = False
        
        # plots after btag , met and mll cut 
        make_FinalSelControlPlots    = True
        make_PlotsforCombinedLimits  = False
        
        # plots for the studies
        make_DiscriminatorPlots      = True
        make_BJetEnRegressionPlots   = False
        make_ttbarEstimationPlots    = False
        
        # the follow are mainly for debugging purposes 
        make_BoostedBtagPlots        = True    
        make_DYReweightingPlots      = False
        make_tau2tau1RatioPlots      = False
        make_deltaRPlots             = False 
        make_InclusivePlots          = False
        make_zoomplotsANDptcuteffect = False
        make_2017Checksplots         = False
        make_LookInsideJets          = False
        

        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                               # more plots to invistagtes 2017 problems  
        #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        if make_2017Checksplots :
            plots += choosebest_jetid_puid(t, muons, electrons, categories, era, sample, isMC)
        
        for channel, (dilepton, catSel) in categories.items():
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                    # Zmass (2Lepton OS && SF ) 
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            
            optstex = ('$e^{+}e^{-}$' if channel=="ElEl" else( '$\mu^{+}\mu^{-}$' if channel=="MuMu" else( '$\mu^{\pm}e^{\pm}$' if channel=="MuEl" else('$e^{+}\mu^{-}$'))))
           
            if self.doYields:
                yield_object.addYields(catSel,"hasOs%s"%channel,"2 OS lep.(%s) + $m_{ll}$ cut"%optstex)
                selections_for_cutflowreport.add(catSel, "2 OS lep.(%s) + $m_{ll}$ cut"%optstex)
            
            if make_InclusivePlots:
                plots += varsCutsPlotsforLeptons(dilepton, catSel, channel)
                plots.extend(cp.makeControlPlotsForZpic(catSel, dilepton, 'oslepSel', channel, 'inclusive'))
                
                inclusiveSel = catSel.refine(f"TwoJet_{channel}_inclusiveSel",cut=[ op.rng_len(AK4jets) >= 2,  op.rng_len(AK8jets) >= 0])
                plots.extend(cp.makeControlPlotsForBasicSel(inclusiveSel,lljj_jets, dilepton, channel, 'inclusive'))
            
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                    # Jets multiplicty  
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            if make_JetmultiplictyPlots :
                for reg, jet in lljj_jets.items():
                    plots.extend(cp.makeJetmultiplictyPlots(catSel, jet, channel,"_NoCutOnJetsLen_" + reg))
                
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            #                                          Control region 
            #                                   *** This's an Inclusive selection *** 
            # I don't care about my CR if boosted and resolved are inclusive , what's matter for me is my SR  ** 
            # boosted is unlikely to have pu jets ; jet pt > 200 in the boosted cat so no pu jets wgt is applied !
            #                                          Signal region 
            # gg fusion :  
            #              resolved :  exactly 2 AK4 b jets 
            #              boosted  :  exactly 1 fat bjet
            # b-associated production : 
            #              resolved : at least 3 AK4 bjets 
            #              boosted  : at least 1 fat bjets && at least 1 AK4 bjets
            # 
            # DY reweighting if set to true , the reweighting will be done here too
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            
            TwoLeptonsTwoJets_Resolved = catSel.refine(f"TwoJet_{channel}Sel_resolved", cut=[ op.rng_len(AK4jets) >= 2,  op.rng_len(AK8jets) == 0])
            
            TwoLeptonsOneJet_Boosted   = catSel.refine(f"OneJet_{channel}Sel_boosted",  cut=[ op.rng_len(AK8jets) >= 1,  op.rng_len(AK4jets) >= 0])
            
            lightflavour_j = {} 
            for reg , sel in {"resolved": TwoLeptonsTwoJets_Resolved, 
                              "boosted": TwoLeptonsOneJet_Boosted
                              }.items():
            
                if self.doDY_reweighting:
                    lightflavour_j[reg] = prepareCP_ForDrellYan0Btag(lljj_cleanjets, jetType, dilepton, sel, channel, reg, era, "medium", corrMET, doMETCut=True, doWgt=False, doSum=True, doPlot=False)
            
                if make_DYReweightingPlots: # Before reweighting ** 
                    dy_cp, dy_cpToSum = ProduceFitPolynomialDYReweighting(lljj_jets[reg], dilepton, sel, channel, reg, sampleCfg, era, isMC, self.reweightDY, self.doSysts, doWgt=True, doSum=True)
                    plots.extend(dy_cp)
                    plots_ToSum2.update(dy_cpToSum)
            
                    cp_0Btag_noDYwgt, cp_0Btag_noDYwgtToSum = prepareCP_ForDrellYan0Btag(lljj_cleanjets, jetType, dilepton, sel, channel, reg, era, "medium", corrMET, doMETCut=True, doWgt=False, doSum=True, doPlot=True)
                    plots.extend(cp_0Btag_noDYwgt)
                    plots_ToSum2.update(cp_0Btag_noDYwgtToSum)
            
            if self.doDY_reweighting:
                if isMC and "group" in sampleCfg.keys() and sampleCfg["group"]=='DY' and channel in ['MuMu', 'ElEl']:
                    jj_mass    = { 'resolved': (AK4jets[0].p4 + AK4jets[1].p4).M(),
                                   'boosted' :  AK8jets[0].p4.M() }

                    lowmass_fitdeg = { '2017': 7, '2016': 6, '2018': 6 } 

                    DYweight_reso = getDYweightFromPolyfit(channel, era_, 'resolved', 'mjj', jj_mass['resolved'], 5, lightflavour_j['resolved'], self.doSysts, self.reweightDY, self.doOnlylightflav)
                    DYweight_boo  = getDYweightFromPolyfit(channel, era_, 'boosted', 'mjj', jj_mass['boosted'], lowmass_fitdeg[era_], lightflavour_j['boosted'], self.doSysts, self.reweightDY, self.doOnlylightflav)
            
                    TwoLeptonsTwoJets_Resolved = TwoLeptonsTwoJets_Resolved.refine(f"TwoJet_{channel}Sel_resolved_DYweight", weight=(DYweight_reso))
                    TwoLeptonsOneJet_Boosted   = TwoLeptonsOneJet_Boosted.refine(f"OneJet_{channel}Sel_boosted_DYweight", weight=(DYweight_boo))
            
            lljjSelections = { "resolved": TwoLeptonsTwoJets_Resolved,
                               "boosted" : TwoLeptonsOneJet_Boosted}

            if self.doDY_reweighting and make_DYReweightingPlots: # After reweighting
                for reg , sel in lljjSelections.items():
                    cp_0Btag_DYwgt, cp_0Btag_DYwgtToSum = prepareCP_ForDrellYan0Btag(lljj_cleanjets, jetType, dilepton, sel, channel, reg, era, "medium", corrMET, doMETCut=True, doWgt=True, doSum=True, doPlot=True)
                    plots.extend(cp_0Btag_DYwgt)
                    plots_ToSum2.update(cp_0Btag_DYwgtToSum)

            if self.doYields:
                for reg, sel in lljjSelections.items():
                    yield_object.addYields(sel, f"{lljj_selName[reg]}_{channel}" , f"2 OS lep.({optstex}) + {jlenOpts['inclusive'][reg]} {jetType[reg]} jets+ $m_{{ll}}$ cut")
                    selections_for_cutflowreport.add(sel, f"2 OS lep.({optstex}) + {jlenOpts['inclusive'][reg]} {jetType[reg]} jets+ $m_{{ll}}$ cut")

            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            
            if make_zoomplotsANDptcuteffect:
                plots.extend(ptcuteffectOnJetsmultiplicty(catSel, dilepton, AK4jets_noptcut, AK4jets, corrMET, era, channel))
                plots.extend(zoomplots(catSel, lljjSelections["resolved"], dilepton, AK4jets, 'resolved', channel))
            
            if make_METPuppiPlots:
                plots.extend(cp.MakePuppiMETPlots(PuppiMET, lljjSelections["resolved"], channel))
            
            if make_LookInsideJets:
                plots.extend(LeptonsInsideJets(AK4jets, lljjSelections["resolved"], channel))

            if make_recoVerticesPlots:
                plots.extend( cp.makePrimaryANDSecondaryVerticesPlots(t, catSel, channel))
           

            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                # Control Plots in boosted and resolved  
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            
            for reg, sel in lljjSelections.items():
                jet = lljj_jets[reg]
                if make_JetmultiplictyPlots:
                    plots.extend(cp.makeJetmultiplictyPlots(sel, jet, channel, reg))
                
                if make_deltaRPlots:    
                    plots.extend(cp.makedeltaRPlots(sel, jet, dilepton, channel, reg))
                
                if make_JetsPlusLeptonsPlots:
                    plots.extend(cp.makeJetPlots(sel, jet, channel, reg, era))
                    plots.extend(cp.makeControlPlotsForBasicSel(sel, jet, dilepton, channel, reg))
                
                if make_ZpicPlots:
                    plots.extend(cp.makeControlPlotsForZpic(sel, dilepton, 'lepplusjetSel', channel, reg))
            
            if make_DiscriminatorPlots:
                for tagger, list_j_sel in {'DeepFlavour'    : [lljj_jets['resolved'], lljjSelections['resolved']],
                                    'DeepCSV'        : [lljj_jets['boosted'], lljjSelections['boosted']],
                                    'DeepDoubleBvLV2': [lljj_jets['boosted'], lljjSelections['boosted']] 
                                    }.items():
                    discr_cp, discr_cpToSum = cp.MakeBtagDiscriminatorPlots(tagger, list_j_sel, channel)
                    plots.extend(discr_cp)
                    plots_ToSum2.update(discr_cpToSum)
            
            if make_tau2tau1RatioPlots:  
                plots.extend(cp.makeNsubjettinessPLots(lljjSelections["boosted"], fatjets_nosubjettinessCut, catSel, channel))
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                    # DeepCSV for both boosted && resolved , DeepFlavour  
            #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            
            Expand_WorkingPoints = { 'L':['L1','L2'], 'M': ['M1', 'M2'], 'T': ['T1', 'T2']}
            
            for j, wp in enumerate(self.WorkingPoints): 
                
                LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo ={}
                LeptonsPlusBjets_NoMETCut_bTagEventWeight_Res ={}
                # bestJetPairs = {}
                # for tagger,bScore in {"DeepCSV": "btagDeepB", "DeepFlavour": "btagDeepFlavB"}.items():
                #     jets_by_score = op.sort(bjets_resolved[tagger][wp], partial((lambda j,bSc=None : -getattr(j, bSc)), bSc=bScore))
                #     bestJetPairs[tagger] = (jets_by_score[0], jets_by_score[1])
                # 
                
                # resolved 
                bJets_resolved_PassdeepflavourWP  = bjets_resolved["DeepFlavour"][wp]
                bJets_resolved_PassdeepcsvWP      = bjets_resolved["DeepCSV"][wp]
                # boosted
                if wp !='T':
                    bJets_boosted_PassdeepcsvWP   = bjets_boosted["DeepCSV"][wp]
                
                if self.dobJetER and isSignal:
                    bJets_resolved_PassdeepflavourWP  = op.map(bJets_resolved_PassdeepflavourWP, lambda j: j.pt*j.bRegCorr)
                    bJets_resolved_PassdeepcsvWP      = op.map(bJets_resolved_PassdeepcsvWP, lambda j: j.pt*j.bRegCorr)
                    #bJets_resolved_PassdeepflavourWP = corr.bJetEnergyRegression( bJets_resolved_PassdeepflavourWP)
                    #bJets_resolved_PassdeepcsvWP     = corr.bJetEnergyRegression( bJets_resolved_PassdeepcsvWP)
                
                if make_JetmultiplictyPlots:
                    bjets = { 'resolved': {
                                    'DeepFlavour': bJets_resolved_PassdeepflavourWP, 
                                    'DeepCSV'    : bJets_resolved_PassdeepcsvWP},
                               'boosted': {} }
                    if wp !='T':
                        bjets['boosted'].update(bJets_boosted_PassdeepcsvWP)

                    for region, dict_ in bjets.items():
                        for tagger, btaggedJets in bjets.items():
                            plots.extend(cp.makeJetmultiplictyPlots(catSel, btaggedJets, channel,f"_NoCutOnbJetsLen_{reg}_{tagger}_{wp}"))
                
                
                if make_BoostedBtagPlots and wp !='T':
                    weight = None
                    if self.doPass_bTagEventWeight and isMC:
                        weight = { 'nb3-boosted': [ run2_bTagEventWeight_PerWP[wp]['bb_associatedProduction']['boosted']['DeepCSV{}'.format(wp)],
                                                    run2_bTagEventWeight_PerWP[wp]['bb_associatedProduction']['resolved']['DeepCSV{}'.format(wp)] 
                                                    ],
                                   'nb2-boosted': run2_bTagEventWeight_PerWP[wp]['gg_fusion']['boosted']['DeepCSV{}'.format(wp)]
                                 }

                    cp_boosted, cp_boostedToSum, cfr = get_bestSubjetsCut(wp , lljjSelections["boosted"], bJets_resolved_PassdeepcsvWP, weight, channel, dilepton, AK8jets, corrMET, optstex, era, self.doProduceSummedPlots, self.BTV_discrCuts)
                    plots.extend(cp_boosted)
                    plots_ToSum2.update(cp_boostedToSum)
                    
                    for scenario, sel_dict in cfr.items():
                        for process, (latex_nm, sel) in sel_dict.items():
                            yield_object.addYields(sel, scenario+f'_{process}', latex_nm)
                            selections_for_cutflowreport.add(sel, latex_nm)
               
               #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                    # No MET cut selections
               #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                LeptonsPlusBjets_NoMETCut_bTagEventWeight_Res = {
                        "gg_fusion": {
                                    "DeepFlavour{}".format(wp) :  lljjSelections["resolved"].refine("TwoLeptonsExactly2Bjets_NoMETcut_NobTagEventWeight_DeepFlavour{}_{}_Resolved".format(wp, channel),
                                                                        cut    = [ op.rng_len(bJets_resolved_PassdeepflavourWP) == 2 ],
                                                                        weight = (run2_bTagEventWeight_PerWP[wp]['gg_fusion']['resolved']['DeepFlavour{}'.format(wp)] if isMC else None) 
                                                                                    if self.doPass_bTagEventWeight else None) },
                        "bb_associatedProduction": {
                                    "DeepFlavour{}".format(wp) :  lljjSelections["resolved"].refine("TwoLeptonsAtLeast3Bjets_NoMETcut_NobTagEventWeight_DeepFlavour{}_{}_Resolved".format(wp, channel),
                                                                        cut    = [ op.rng_len(bJets_resolved_PassdeepflavourWP) >= 3 ],
                                                                        weight = (run2_bTagEventWeight_PerWP[wp]['bb_associatedProduction']['resolved']['DeepFlavour{}'.format(wp)] if isMC else None) 
                                                                                    if self.doPass_bTagEventWeight else None) },
                            }
    
                if wp !='T':
                    LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo = {
                        "gg_fusion": {
                                    "DeepCSV{}".format(wp)     :  lljjSelections["boosted"].refine("TwoLeptonsExactly1FatBjets_NoMETcut_NobTagEventWeight_DeepCSV{}_{}_Boosted".format(wp, channel),
                                                                        cut    = [ op.rng_len(bJets_boosted_PassdeepcsvWP) == 1 ],
                                                                        weight = ( run2_bTagEventWeight_PerWP[wp]['gg_fusion']['boosted']['DeepCSV{}'.format(wp)] if isMC else None) 
                                                                                   if self.doPass_bTagEventWeight else None) },
                        "bb_associatedProduction": {
                                    "DeepCSV{}".format(wp)     :  lljjSelections["boosted"].refine("TwoLeptonsAtLeast1FatBjets1Bresolvedjet_with_AtLeast1AK4_NoMETcut_NobTagEventWeight_DeepCSV{}_{}_Boosted".format(wp, channel),
                                                                        cut    = [ op.rng_len(bJets_boosted_PassdeepcsvWP) >= 1, op.rng_len(bJets_resolved_PassdeepcsvWP) >= 0],
                                                                        weight = ([ run2_bTagEventWeight_PerWP[wp]['bb_associatedProduction']['boosted']['DeepCSV{}'.format(wp)], 
                                                                                    run2_bTagEventWeight_PerWP[wp]['bb_associatedProduction']['resolved']['DeepCSV{}'.format(wp)] ] if isMC else None) 
                                                                                    if self.doPass_bTagEventWeight else None) },
                            }
                

                if self.doDDBvsL:
                    if wp =='T': 
                        LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo["gg_fusion"] = {}
                        LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo["bb_associatedProduction"] = {}
                    
                    if self.BTV_discrCuts and wp in Expand_WorkingPoints.keys():    
                        DDB_WorkingPoints = Expand_WorkingPoints[wp]
                    else:
                        DDB_WorkingPoints = ['custom'] 
                    
                    for wp2 in DDB_WorkingPoints:
                        if wp2 !='custom':
                            discr_cut = orr.BoostedTopologiesWP["DeepDoubleBvLV2"][wp2]
                        else:
                            discr_cut = 0.4
                            if j !=0: continue # filled already so skip 
                        
                        print(f"::: b flavour boosted-DeepDoubleBvLV2{wp2}, discriminator_cut = {discr_cut}" )

                        bJets_boosted_PassdeepdoubleBvsLWP = get_DeepDoubleX(AK8jets, 'btagDDBvLV2', discr_cut)
                        lljj_bJets['boosted']["DeepDoubleBvLV2"][wp2] = bJets_boosted_PassdeepdoubleBvsLWP
                        
                        LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo["gg_fusion"].update({
                                "DeepDoubleBvLV2{}".format(wp2)     :  lljjSelections["boosted"].refine("TwoLeptonsExactly1FatBjets_NoMETcut_NobTagEventWeight_DeepDoubleBvLV2{}_{}_Boosted".format(wp2, channel),
                                                                                cut    = [ op.rng_len(bJets_boosted_PassdeepdoubleBvsLWP) == 1 ],
                                                                                #weight=( get_BoostedEventWeight(era, 'DeepDoubleBvLV2', wp, bJets_boosted_PassdeepdoubleBvsLWP) if isMC else None))
                                                                                weight = None)} )
                        
                        LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo["bb_associatedProduction"].update({
                                "DeepDoubleBvLV2{}".format(wp2)     :  lljjSelections["boosted"].refine("TwoLeptonsAtLeast1FatBjets_with_AtLeast1AK4_NoMETcut_NobTagEventWeight_DeepDoubleBvLV2{}_{}_Boosted".format(wp2, channel),
                                                                                cut    = [ op.rng_len(bJets_boosted_PassdeepdoubleBvsLWP) >= 1 ], 
                                                                                weight = None)} ) 


                llbbSelections_noMETCut = { "gg_fusion":{ 
                                                    "resolved": LeptonsPlusBjets_NoMETCut_bTagEventWeight_Res["gg_fusion"],
                                                    "boosted" : LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo["gg_fusion"] },
                                             "bb_associatedProduction":{ 
                                                    "resolved": LeptonsPlusBjets_NoMETCut_bTagEventWeight_Res["bb_associatedProduction"], 
                                                    "boosted" : LeptonsPlusBjets_NoMETCut_bTagEventWeight_Boo["bb_associatedProduction"] }
                                            }
                if self.doYields:
                    for process, allsel_fortaggerWP_per_reg_and_process in llbbSelections_noMETCut.items():
                        reco_cat, prod = getProduction(process)
                        met_pt_cut  = '$p_{T}^{miss}$ cut'
                        
                        for reg,  dic_selections in allsel_fortaggerWP_per_reg_and_process.items():
                            for taggerWP, sel in dic_selections.items():
                                
                                taggerWP = taggerWP.replace('DeepFlavour', 'DeepJet')
                                
                                yield_object.addYields(sel, f"has2Lep_2{reg}BJets_NoMETCut_{channel}_{taggerWP}_{prod}",
                                        f"{reco_cat} -{reg}: {optstex} + {jlenOpts['exclusive'][prod][reg]} {jetType[reg]} b-jets {reg} ( {taggerWP} ) + no {met_pt_cut}")
                                
                                selections_for_cutflowreport.add(sel, f"{reco_cat} -{reg}: {optstex} + {jlenOpts['exclusive'][prod][reg]} {jetType[reg]} b-jets {reg} ( {taggerWP} ) + no {met_pt_cut}") 
                
                #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                    #  refine previous selections for SR : with MET cut  < 80. 
                #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                llbbSelections = { process: 
                                        { reg:
                                            { key: selNoMET.refine(f"TwoLeptonsTwoBjets_METCut_bTagEventWeight_{key}_{channel}_{reg}_{process}", cut=[ corrMET.pt < optimizeMETcut[process][reg] ])
                                            for key, selNoMET in noMETSels.items() }
                                        for reg, noMETSels in llbbSelections_noMETCut_per_process.items() }
                                    for process, llbbSelections_noMETCut_per_process in llbbSelections_noMETCut.items() 
                                }
                #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                # make Skimmer
                #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                if self.doSkim:
                    for process , Selections_per_process in llbbSelections.items():
                        reco_cat, prod = getProduction(process)
                        for reg, Selections_per_taggerWP in Selections_per_process.items():
                            for taggerWP, FinalSel in Selections_per_taggerWP.items():

                                if reg =="resolved":
                                    bJets  = bjets_resolved[taggerWP.replace(wp, "")][wp]
                                    llbb_M = (dilepton[0].p4 +dilepton[1].p4+bJets[0].p4+bJets[1].p4).M()
                                    bb_M   = op.invariant_mass(bJets[0].p4+bJets[1].p4)
                                    
                                elif reg =="boosted":
                                    bJets  = bjets_boosted[taggerWP.replace(wp, "")][wp]
                                    llbb_M = (dilepton[0].p4 +dilepton[1].p4+bJets[0].p4).M()
                                    bb_M   = bJets[0].mass
                                    bb_softDropM = bJets[0].msoftdrop
                                else:
                                    raise RuntimeError(f'what is going on here ?? ')

                                plots.append(Skim(  f"LepPlusJetsSel_{prod}_{reg}_{channel.lower()}_{taggerWP.lower()}", {
                                        # just copy the variable as it is in the nanoAOD input
                                        "run"            : None,
                                        "event"          : None,
                                        "luminosityBlock": None,  
                                        
                                        "l1_charge"      : dilepton[0].charge,
                                        "l2_charge"      : dilepton[1].charge,
                                        "l1_pdgId"       : dilepton[0].pdgId,
                                        "l2_pdgId"       : dilepton[1].pdgId,
                                        'bb_M'           : bb_M,
                                        'llbb_M'         : llbb_M,
                                        'bb_M_squared'   : op.pow(bb_M, 2),
                                        'llbb_M_squared' : op.pow(llbb_M, 2),
                                        'bb_M_x_llbb_M'  : op.product(bb_M, llbb_M),
                                        
                                        'isResolved'     : op.c_bool(reg == 'resolved'), 
                                        'isBoosted'      : op.c_bool(reg == 'boosted'), 
                                        'isElEl'         : op.c_bool(channel == 'ElEl'), 
                                        'isMuMu'         : op.c_bool(channel == 'MuMu'), 
                                        'isggH'          : op.c_bool(prod == 'ggH'), 
                                        'isbbH'          : op.c_bool(prod == 'bbH'), 

                                        'era'            : op.c_int(int(era_)),
                                        'total_weight'   : FinalSel.weight,
                                        'PU_weight'      : self.PUWeight if isMC else op.c_float(1.), 
                                        'MC_weight'      : t.genWeight if isMC else op.c_float(1.),

                                        f'nB_{jetType[reg]}bJets': op.static_cast("UInt_t", op.rng_len(bJets))
                                    }, FinalSel))
                
                else:
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                        #  to optimize the MET cut 
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                    # you should get them for both signal && bkg  
                    if make_METPlots:
                        for process , Selections_noMETCut_per_process in llbbSelections_noMETCut.items():
                            for reg, sel in Selections_noMETCut_per_process.items():
                                plots.extend(cp.MakeMETPlots(sel, dilepton, corrMET, MET, channel, reg, process))
                    
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                            # Evaluate the training  
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                    if self.doEvaluate:
                        
                        outdir   = os.path.abspath(os.path.join(zaPath, 'config' ))
                        dict_allmasspoints = utils.getSignalMassPoints(outdir)
                        
                        plotOptions = utils.getOpts(channel)
                        if self.doBlinded:
                            plotOptions["blinded-range"] = [0.6, 1.0] 

                        for process, Selections_per_process in llbbSelections.items():
                            for region, selections_per_region in Selections_per_process.items(): 
                                for tag_plus_wp, sel in selections_per_region.items():
                                    
                                    reco_cat, prod = getProduction(process) 
                                    bjets_   = lljj_bJets[region][tag_plus_wp.replace(wp,'')][wp]
                                    jj_p4    = ( (bjets_[0].p4 + bjets_[1].p4) if region=="resolved" else( bjets_[0].p4))
                                    lljj_p4  = ( dilepton[0].p4 + dilepton[1].p4 + jj_p4)
                                    
                                    bb_M     = jj_p4.M()
                                    llbb_M   = lljj_p4.M()
                                    
                                    for mode in [ 'HToZA', 'AToZH']:
                                        if self.SplitSignalPoints: # Between resolved and boosted !
                                            masses_seen_forEvaluation = dict_allmasspoints[process][region][mode]
                                        else:
                                            masses_seen_forEvaluation = dict_allmasspoints[process]['resolved'][mode] + dict_allmasspoints[process]['boosted'][mode]

                                        
                                        signal_grid = { 'seen_byDNN'   : masses_seen_forEvaluation,
                                                        'notseen_byDNN': masses_notseen }
                                    
                                        for k, tup in signal_grid.items():
                                            for parameters in tup: 
                                                if mode == 'HToZA':
                                                    nm = 'ZA'
                                                    mH = parameters[0]
                                                    mA = parameters[1]
                                                    histNm = f"DNNOutput_{nm}node_{channel}_{region}_{tag_plus_wp}_METCut_{process}_MH_{mass_to_str(mH)}_MA_{mass_to_str(mA)}"
                                                else:
                                                    nm = 'ZH'
                                                    mA = parameters[0]
                                                    mH = parameters[1]
                                                    histNm = f"DNNOutput_{nm}node_{channel}_{region}_{tag_plus_wp}_METCut_{process}_MA_{mass_to_str(mA)}_MH_{mass_to_str(mH)}"
                                                
                                                inputsCommon = {'l1_pdgId'        : dilepton[0].pdgId               ,
                                                                'myera'           : op.c_int(int(era_))             ,
                                                                'bb_M'            : jj_p4.M()                       ,
                                                                'llbb_M'          : lljj_p4.M()                     ,
                                                                'bb_M_squared'    : op.pow(bb_M, 2)                 ,
                                                                'llbb_M_squared'  : op.pow(llbb_M, 2)               ,
                                                                'bb_M_x_llbb_M'   : op.product(bb_M, llbb_M)        ,
                                                                'mA'              : op.c_float(mA)                  ,
                                                                'mH'              : op.c_float(mH)                  ,  
                                                                'isResolved'      : op.c_bool(region == 'resolved') ,
                                                                'isBoosted'       : op.c_bool(region == 'boosted')  ,
                                                                'isggH'           : op.c_bool(prod == 'ggH')    ,
                                                                'isbbH'           : op.c_bool(prod == 'bbH')    ,
                                                                }
                                                
                                                if self.rebin == 'bayesian':
                                                    # I did not optimize the bayesian blocks for these catagories yet !!# FIXME
                                                    look_for = histNm
                                                    if region == 'boosted':
                                                        look_for = look_for.replace('boosted', 'resolved')
                                                    if process == 'bb_associatedProduction':
                                                        look_for = look_for.replace('bb_associatedProduction', 'gg_fusion')
                                                    
                                                    print(f"working on rebinning of {histNm} :: bayesian blocks {bayesian_blocks_binnings['histograms'][look_for][0][0]}") 
                                                    binning = VarBin(bayesian_blocks_binnings['histograms'][look_for][0][0])
                                                
                                                elif self.rebin == 'uniform':
                                                    binning = EqB(50, 0., 1.)
    
                                                DNN_Inputs   = [op.array("float",val) for val in inputStaticCast(inputsCommon,"float")]
                                                DNN_Output   = ZA_mvaEvaluator(*DNN_Inputs) # [DY, TT, ZA or ZH]
                                                
                                                # some crap , ignore !! 
                                                #find_idx_maxProb = op.rng_max_element_index(DNN_Output)
                                                #sel= sel.refine(f'{histNm}_sel', cut=[find_idx_maxProb == op.c_int(2)])                
                                                
                                                pltToSum_OSSFLepFlav = Plot.make1D(histNm, DNN_Output[2], sel, binning, title=f'DNN_Output {nm}', plotopts=plotOptions)
                                                plots += [pltToSum_OSSFLepFlav]
                                                
                                                if not channel in ['MuEl', 'ElMu']:
                                                    plots_ToSum[(histNm.replace(channel, 'OSSF'))].append(pltToSum_OSSFLepFlav)
                                                
                                                #plots.append(Plot.make2D(f"mbb_vs_{histNm}",
                                                #            (jj_p4.M(), DNN_Output[2]), sel,
                                                #            (EqB(50, 0., 1000.), EqB(50, 0., 1.)),
                                                #            title="mbb mass Input vs DNN Output", plotopts=plotOptions))
                                                #plots.append(Plot.make2D(f"mllbb_vs_{histNm}",
                                                #            (lljj_p4.M(), DNN_Output[2]), sel,
                                                #            (EqB(50, 0., 1000.), EqB(50, 0., 1.)),
                                                #            title="mllbb mass Input vs DNN Output", plotopts=plotOptions))
                                                
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                        #  TTbar Esttimation  
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                    if make_ttbarEstimationPlots:
                        # High met is Only included in this part for ttbar studies 
                        for process, Selections_per_process in llbbSelections.items():
                            for metReg, sel in {
                                    "METCut" : Selections_per_process["resolved"],
                                    "HighMET": {key: selNoMET.refine("TwoLeptonsTwoBjets_{}_{}_Resolved_with_inverted_METcut_{}".format(key, channel, process),
                                        cut=[ corrMET.pt > optimizeMETcut[process]['resolved'] ])
                                        for key, selNoMET in llbbSelections_noMETCut[process]["resolved"].items() }
                                    }.items():
                                plots.extend(cp.makeHistosForTTbarEstimation(sel, dilepton, bjets_resolved, corrMET, MET, channel, "resolved", metReg, process, self.BTV_discrCuts))
                    
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                                                        #  Control Plots for  Final selections
                    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                    llbb_metCut_forPlots = {}

                    if make_bJetsPlusLeptonsPlots_METcut :
                        llbb_metCut_forPlots["METCut"] = llbbSelections
                    if make_bJetsPlusLeptonsPlots_NoMETcut :
                        llbb_metCut_forPlots["NoMETCut"] = llbbSelections_noMETCut
                    
                    for metCutNm, metCutSelections_llbb in llbb_metCut_forPlots.items():
                        bTagtWeight_status = "bTagWgt" if self.doPass_bTagEventWeight else "NobTagWgt"
                        bJER_status = "bJetER" if self.dobJetER else "NobJetER"
                        metCutNm_   = f"{metCutNm}_{bJER_status}_{bTagtWeight_status}"
                        met_pt_cut  = '$p_{T}^{miss}$ cut' if metCutNm == 'METCut' else ''
                        
                        for process, metCutSelections_llbb_per_process in metCutSelections_llbb.items():
                            reco_cat, prod =getProduction(process)

                            for reg, selDict in metCutSelections_llbb_per_process.items():
                                bjets = lljj_bJets[reg]
                                
                                if make_FinalSelControlPlots:
                                    final_cp, final_cpToSum = cp.makeControlPlotsForFinalSel(selDict, bjets, dilepton, channel, reg, metCutNm_, process, self.doProduceSummedPlots, self.BTV_discrCuts)
                                    bjets_cp, bjets_cpToSum = cp.makeBJetPlots(selDict, bjets, channel, reg, metCutNm_, era, process, self.doProduceSummedPlots, self.BTV_discrCuts)
                                    
                                    plots.extend(final_cp)
                                    plots.extend(bjets_cp)
                                    
                                    plots_ToSum2.update(final_cpToSum)
                                    plots_ToSum2.update(bjets_cpToSum)

                                if make_PlotsforCombinedLimits:
                                    plots.extend(makerhoPlots(selDict, bjets, dilepton, self.ellipses, self.ellipse_params, reg, metCutNm_, wp, channel, self.doBlinded, process))

                                if make_BJetEnRegressionPlots and reg == 'resolved':
                                    plots.extend(cp.MakeBJERcorrComparaisonPlots(selDict, bjets, dilepton, channel, reg, metCutNm_, process, self.BTV_discrCuts))
                                
                                if self.doYields and metCutNm == 'METCut':
                                    for key, sel in selDict.items():
                                        key = key.replace('DeepFlavour', 'DeepJet')
                                        yield_object.addYields(sel, f"has2Lep_2{reg}BJets_{metCutNm_}_{channel}_{key}_{prod}",
                                                f"{reco_cat} -{reg}: {optstex} + {jlenOpts['exclusive'][prod][reg]} {jetType[reg]} b-jets {reg} ({key}) + {met_pt_cut}")
                                        
                                        selections_for_cutflowreport.add(sel, f"{reco_cat} -{reg}: {optstex} + {jlenOpts['exclusive'][prod][reg]} {jetType[reg]} b-jets ({key}) + {met_pt_cut}")
                    
        
        if self.doYields:
            plots.extend(yield_object.returnPlots())
        
        for dict_ in [plots_ToSum, plots_ToSum2]:
            for pkey, plt in dict_.items():
                if plt:
                    plots.append(SummedPlot(pkey, plt, plotopts=utils.getOpts("ossf")))
            
        return plots

    def postProcess(self, taskList, config=None, workdir=None, resultsdir=None):
        # run plotIt as defined in HistogramsModule - this will also ensure that self.plotList is present
        super(NanoHtoZA, self).postProcess(taskList, config, workdir, resultsdir)

        import json 
        import bambooToOls
        import pandas as pd
        
        from bamboo.root import gbl
        from bamboo.plots import CutFlowReport, DerivedPlot, Skim
        from plotit.plotit import Stack
        from bamboo.analysisutils import loadPlotIt

        outDir = os.path.join(resultsdir, "normalizedSummedSignal")
        if os.path.isdir(outDir): 
            shutil.rmtree(outDir)
        os.makedirs(outDir)
        #FIXME
        #utils.run_Plotit(workdir, resultsdir, outDir, self.readCounters, config)
        
        if not self.plotList:
            self.plotList = self.getPlotList(resultsdir=resultsdir, config=config)
        
        # memory usage
        #start= timer()
        #end= timer()
        #maxrssmb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
        #logger.info(f"{len(self.plotList):d} plots defined in {end - start:.2f}s, max RSS: {maxrssmb:.2f}MB")
        #with open(os.path.join(resultsdir=".","memoryusage.json"%suffix), "w") as handle:
        #    json.dump(len(self.plotList), handle, indent=4)
        #    json.dump(maxrssmb, handle, indent=4)
        #    json.dump(end - start, handle, indent=4)

        if self.doSysts:
            for task in taskList:
                if self.isMC(task.name) and "syst" not in task.config:
                    if self.pdfVarMode == "full" and task.config.get("pdf_full", False):
                        utils.producePDFEnvelopes(self.plotList, task, resultsdir)
        
        plotstoNormalized = []
        for plots in self.plotList:
            if plots.name.startswith('rho_steps_') or plots.name.startswith('jj_M_') or plots.name.startswith('lljj_M_') or plots.name.startswith('DNNOutput_'):
                plotstoNormalized.append(plots)
        if not os.path.isdir(os.path.join(resultsdir, "normalizedForCombined")):
            os.makedirs(os.path.join(resultsdir,"normalizedForCombined"))

        if plotstoNormalized:
            utils.normalizeAndMergeSamplesForCombined(plotstoNormalized, self.readCounters, config, resultsdir, os.path.join(resultsdir, "normalizedForCombined"))
        
        # save generated-events for each samples--- > mainly needed for the DNN
        plotList_cutflowreport = [ ap for ap in self.plotList if isinstance(ap, CutFlowReport) ]
        #bambooToOls.SaveCutFlowReports(config, plotList_cutflowreport, resultsdir, self.readCounters)
        
        prepostVFP_xsec = dict()
        prepostVFP_sumw = dict()
        for era in config["eras"]:
            xsec = dict()
            sumw = dict()
            for smpNm, smpCfg in config["samples"].items():
                outName = f"{smpNm}.root"
                if 'data' in smpCfg.values(): 
                    continue
                if smpCfg["era"] != era:
                    continue
                f = gbl.TFile.Open(os.path.join(resultsdir, outName))
                xsec[outName]  = smpCfg["cross-section"]
                sumw[outName]  = self.readCounters(f)[smpCfg["generated-events"]]
                if 'VFP' in smpNm:
                    prepostVFP_xsec[outName]  = smpCfg["cross-section"]
                    prepostVFP_sumw[outName]  = self.readCounters(f)[smpCfg["generated-events"]]

            xsecSumw_dir = os.path.join(resultsdir, "data")
            if not os.path.isdir(xsecSumw_dir):
                os.makedirs(xsecSumw_dir)
            
            with open(os.path.join(xsecSumw_dir, f"ulegacy{era}_xsec.json"), "w") as normF:
                json.dump(xsec, normF, indent=4)
            with open(os.path.join(xsecSumw_dir, f"ulegacy{era}_event_weight_sum.json"), "w") as normF:
                json.dump(sumw, normF, indent=4)

        with open(os.path.join(xsecSumw_dir, f"ulegacy2016_xsec.json"), "w") as normF:
            json.dump(prepostVFP_xsec, normF, indent=4)
        with open(os.path.join(xsecSumw_dir, f"ulegacy2016_event_weight_sum.json"), "w") as normF:
            json.dump(prepostVFP_sumw, normF, indent=4)

        
        plotList_2D = [ ap for ap in self.plotList if ( isinstance(ap, Plot) or isinstance(ap, DerivedPlot) ) and len(ap.binnings) == 2 ]
        logger.debug("Found {0:d} plots to save".format(len(plotList_2D)))

        p_config, samples, plots_2D, systematics, legend = loadPlotIt(config, plotList_2D, eras=None, workdir=workdir, resultsdir=resultsdir, readCounters=self.readCounters, vetoFileAttributes=self.__class__.CustomSampleAttributes, plotDefaults=self.plotDefaults)
        
        for plot in plots_2D:
            if ('_2j_jet_pt_eta_') in plot.name  or plot.name.startswith('pair_lept_2j_jet_pt_vs_eta_'):
                expStack = Stack(smp.getHist(plot) for smp in samples if smp.cfg.type == "MC")
                cv = gbl.TCanvas(f"c{plot.name}")
                cv.cd(1)
                expStack.obj.Draw("COLZ0")
                cv.Update()
                cv.SaveAs(os.path.join(resultsdir, f"{plot.name}.png"))
            else:
                logger.debug(f"Saving plot {plot.name}")
                obsStack = Stack(smp.getHist(plot) for smp in samples if smp.cfg.type == "DATA")
                expStack = Stack(smp.getHist(plot) for smp in samples if smp.cfg.type == "MC")
                cv = gbl.TCanvas(f"c{plot.name}")
                cv.Divide(2)
                if not not expStack:
                    cv.cd(1)
                    expStack.obj.Draw("COLZ0")
                if not not obsStack:
                    cv.cd(2)
                    obsStack.obj.Draw("COLZ0")
                cv.Update()
                cv.SaveAs(os.path.join(resultsdir, f"{plot.name}.png"))
        
        if self.doProduceParquet:
            skims = [ap for ap in self.plotList if isinstance(ap, Skim)]
            if self.doSkim and skims:
                try:
                    for skim in skims:
                        frames = []
                        for smp in samples:
                            for cb in (smp.files if hasattr(smp, "files") else [smp]):  # could be a helper in plotit
                                # Take specific columns
                                tree = cb.tFile.Get(skim.treeName)
                                if not tree:
                                    print( f"KEY TTree {skim.treeName} does not exist, we are gonna skip this {smp}\n")
                                else:
                                    N = tree.GetEntries()
                                    # https://indico.cern.ch/event/775679/contributions/3244724/attachments/1767054/2869505/RDataFrame.AsNumpy.pdf
                                    # https://stackoverflow.com/questions/33813815/how-to-read-a-parquet-file-into-pandas-dataframe
                                    #print (f"Entries in {smp} // KEY TTree {skim.treeName}: {N}")
                                    cols = gbl.ROOT.RDataFrame(cb.tFile.Get(skim.treeName)).AsNumpy()
                                    cols["total_weight"] *= cb.scale
                                    cols["process"] = [smp.name]*len(cols["total_weight"])
                                    frames.append(pd.DataFrame(cols))
                        df = pd.concat(frames)
                        df["process"] = pd.Categorical(df["process"], categories=pd.unique(df["process"]), ordered=False)
                        pqoutname = os.path.join(resultsdir, f"{skim.name}.parquet")
                        df.to_parquet(pqoutname)
                        logger.info(f"Dataframe for skim {skim.name} saved to {pqoutname}")
                except ImportError as ex:
                    logger.error("Could not import pandas, no dataframes will be saved")
        