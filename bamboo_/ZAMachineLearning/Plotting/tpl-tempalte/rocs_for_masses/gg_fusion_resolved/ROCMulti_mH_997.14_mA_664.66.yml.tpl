ROC_mH_997.14_mA_664.66:
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
  title : ( $M_H$, $M_A$)= ( 997.14, 664.66) GeV
  cut : 'mH==997 & mA==664 & isggH & isResolved'
  selector :
    'TT' : 'TT'
    'DY' : 'DY'
    'ZA' : 'ZA'
