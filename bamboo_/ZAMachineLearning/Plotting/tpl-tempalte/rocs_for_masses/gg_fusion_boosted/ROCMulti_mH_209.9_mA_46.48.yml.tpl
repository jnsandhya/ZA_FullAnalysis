ROC_mH_209.9_mA_46.48:
  tree: tree
  classes:
    - DY
    - TT
    - ZA
  prob_branches:
    - output_DY
    - output_TT
    - output_ZA
  labels:
    - P(DY | x,$\theta$)
    - P($t\bar{t}$ | x,$\theta$)
    - P($H\rightarrow ZA$ | x,$\theta$)
  colors:
    - navy
    - darkred
    - green
  weight : event_weight
  title : ( $M_H$, $M_A$)= ( 209.9, 46.48) GeV
  cut : 'mH==209 & mA==46 & isggH & isBoosted'
  selector :
    'TT' : 'TT'
    'DY' : 'DY'
    'ZA' : 'ZA'
