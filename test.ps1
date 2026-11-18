$ObjectId = "5f489532-a978-468f-be04-7cff341e5171"
$RoleName = "Reader"

$FolderNames = @(
    "Unilever Sandbox",
    "AI Foundation Independent",
    "Automation Factory-01",
    "Unilever Independent",
    "Azure Migration-01",
    "B2B Commerce-01",
    "Collaboration-01",
    "Communication Services-01",
    "Consumer Engagement-01",
    "Consumer Performance-01",
    "Consumer Service and Operations-01",
    "Core Data Ecosystem-01",
    "D&A Independent",
    "Corporate Functions-01",
    "CPS Product Testing-01",
    "Digital Employee Services-01",
    "Unilever Experimental",
    "File Sync-01",
    "Foundation Services-01",
    "Unilever Restricted",
    "Frontier-01",
    "Global Finance-01",
    "Global Supply Chain-01",
    "Geography IT-01",
    "Health and Wellbeing-01",
    "HR-01",
    "Integration-01",
    "Leveredge-01",
    "Master Data-01",
    "Messaging, ID and Access Management-01",
    "Mobile and Device Services-01",
    "NorthAmerica-01",
    "Operational Excellence and Controls-01",
    "Procurement-01",
    "Unilever Root",
    "R&D-In Silico First eScience-01",
    "R&D-PLM & Team Centre-01",
    "SAP Technical Services-01",
    "Security-01",
    "Solution Manager and GRC-01",
    "Support Tools and ICM-01",
    "Sustainability and Communications-01",
    "Testing SubCreation-01",
    "Market Operations-01",
    "Unilever International-01"
) | Sort-Object -Unique

foreach ($FolderName in $FolderNames) {

    $MG = Get-AzManagementGroup | Where-Object { $_.DisplayName -eq $FolderName }

    if ($MG) {
        $Scope = "/providers/Microsoft.Management/managementGroups/$($MG.Name)"

        New-AzRoleAssignment `
            -ObjectId $ObjectId `
            -RoleDefinitionName $RoleName `
            -Scope $Scope

        Write-Host "Assigned role on $FolderName"
    }
    else {
        Write-Warning "$FolderName not found"
    }
}